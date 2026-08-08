package rules

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var (
	ErrNotFound         = errors.New("rule revision not found")
	ErrConflict         = errors.New("rule revision conflict")
	ErrSourceIncomplete = errors.New("rule source is incomplete")
)

type Revision struct {
	ID             string          `json:"id"`
	SiteID         string          `json:"site_id"`
	SiteCode       string          `json:"site_code"`
	Revision       int             `json:"revision"`
	Status         string          `json:"status"`
	Fingerprint    string          `json:"fingerprint"`
	SourceURL      string          `json:"source_url"`
	CapturedAt     *time.Time      `json:"captured_at,omitempty"`
	MarkdownPath   string          `json:"markdown_path"`
	MarkdownSHA256 string          `json:"markdown_sha256"`
	Policy         json.RawMessage `json:"policy"`
	Obligations    json.RawMessage `json:"obligations"`
	CreatedAt      time.Time       `json:"created_at"`
}

type SiteSummary struct {
	ID                    string `json:"id"`
	Code                  string `json:"code"`
	Name                  string `json:"name"`
	Adapter               string `json:"adapter"`
	Enabled               bool   `json:"enabled"`
	LiveValidationStatus  string `json:"live_validation_status"`
	ActiveRuleRevisionID  string `json:"active_rule_revision_id,omitempty"`
	ActiveRuleFingerprint string `json:"active_rule_fingerprint,omitempty"`
}

type Store struct {
	pool *pgxpool.Pool
	root string
}

func NewStore(pool *pgxpool.Pool, dataDir string) (*Store, error) {
	root := filepath.Join(dataDir, "rules")
	if !filepath.IsAbs(root) {
		return nil, fmt.Errorf("rule root must be absolute")
	}
	if err := os.MkdirAll(root, 0o750); err != nil {
		return nil, fmt.Errorf("create rule root: %w", err)
	}
	return &Store{pool: pool, root: root}, nil
}

func (s *Store) Import(ctx context.Context, raw []byte, actor workflow.Actor) (Revision, error) {
	document, err := ParseMarkdown(raw)
	if err != nil {
		return Revision{}, err
	}
	fingerprint, err := document.Fingerprint()
	if err != nil {
		return Revision{}, err
	}
	policy, err := document.PolicyJSON()
	if err != nil {
		return Revision{}, fmt.Errorf("serialize rule policy: %w", err)
	}
	obligations, err := json.Marshal(document.Obligations)
	if err != nil {
		return Revision{}, fmt.Errorf("serialize rule obligations: %w", err)
	}
	rawHash := sha256.Sum256(raw)
	markdownSHA := hex.EncodeToString(rawHash[:])
	relativePath := filepath.ToSlash(filepath.Join(document.Site.Code, markdownSHA+".md"))
	if err := s.writeImmutable(relativePath, raw); err != nil {
		return Revision{}, err
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Revision{}, fmt.Errorf("begin rule import transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var siteID string
	err = tx.QueryRow(ctx, "SELECT id::text FROM sites WHERE code = $1 FOR UPDATE", document.Site.Code).Scan(&siteID)
	if errors.Is(err, pgx.ErrNoRows) {
		return Revision{}, fmt.Errorf("%w: site %s is not registered", ErrNotFound, document.Site.Code)
	}
	if err != nil {
		return Revision{}, fmt.Errorf("lock rule site: %w", err)
	}
	existing, err := scanRevision(tx.QueryRow(ctx, revisionSelect+" WHERE sr.site_id = $1 AND sr.fingerprint = $2", siteID, fingerprint))
	if err == nil {
		if err := tx.Commit(ctx); err != nil {
			return Revision{}, fmt.Errorf("commit idempotent rule import: %w", err)
		}
		return existing, nil
	}
	if !errors.Is(err, ErrNotFound) {
		return Revision{}, err
	}
	var revisionNumber int
	if err := tx.QueryRow(ctx, "SELECT COALESCE(max(revision), 0) + 1 FROM site_rule_revisions WHERE site_id = $1", siteID).Scan(&revisionNumber); err != nil {
		return Revision{}, fmt.Errorf("allocate rule revision: %w", err)
	}
	var revision Revision
	createdBy := nullableUUID(actor.ID)
	err = tx.QueryRow(ctx, `
		INSERT INTO site_rule_revisions(
			site_id, revision, status, fingerprint, source_url, captured_at,
			markdown_path, markdown_sha256, parsed_policy, obligations, created_by
		)
		VALUES ($1, $2, 'draft', $3, $4, $5, $6, $7, $8, $9, $10)
		RETURNING id::text, created_at`,
		siteID, revisionNumber, fingerprint, document.Source.URL, parseCapturedAt(document.Source.CapturedAt),
		relativePath, markdownSHA, policy, obligations, createdBy,
	).Scan(&revision.ID, &revision.CreatedAt)
	if err != nil {
		return Revision{}, fmt.Errorf("insert rule revision: %w", err)
	}
	revision.SiteID = siteID
	revision.SiteCode = document.Site.Code
	revision.Revision = revisionNumber
	revision.Status = "draft"
	revision.Fingerprint = fingerprint
	revision.SourceURL = document.Source.URL
	revision.MarkdownPath = relativePath
	revision.MarkdownSHA256 = markdownSHA
	revision.Policy = policy
	revision.Obligations = obligations
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ($1, NULLIF($2, ''), 'site_rule.import', 'site_rule_revision', $3, $4)`,
		actor.Type, actor.ID, revision.ID, mustJSON(map[string]any{
			"site": document.Site.Code, "revision": revisionNumber, "fingerprint": fingerprint,
			"source_complete": document.Source.Complete,
		}),
	); err != nil {
		return Revision{}, fmt.Errorf("audit rule import: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return Revision{}, fmt.Errorf("commit rule import: %w", err)
	}
	return revision, nil
}

func (s *Store) Approve(ctx context.Context, revisionID, expectedFingerprint, comment string, actor workflow.Actor) (Revision, error) {
	reviewerID, err := uuid.Parse(actor.ID)
	if err != nil {
		return Revision{}, fmt.Errorf("reviewer must be an authenticated user")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Revision{}, fmt.Errorf("begin rule approval transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	revision, err := scanRevision(tx.QueryRow(ctx, revisionSelect+" WHERE sr.id = $1 FOR UPDATE", revisionID))
	if err != nil {
		return Revision{}, err
	}
	if revision.Status != "draft" {
		return Revision{}, fmt.Errorf("%w: rule revision is %s", ErrConflict, revision.Status)
	}
	if revision.Fingerprint != expectedFingerprint {
		return Revision{}, fmt.Errorf("%w: rule fingerprint does not match", ErrConflict)
	}
	var policy struct {
		Source Source `json:"source"`
	}
	if err := json.Unmarshal(revision.Policy, &policy); err != nil {
		return Revision{}, fmt.Errorf("decode rule policy: %w", err)
	}
	if !policy.Source.Complete {
		return Revision{}, fmt.Errorf("%w: complete source text must be supplied before approval", ErrSourceIncomplete)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO site_rule_approvals(rule_revision_id, reviewer_id, decision, comment, fingerprint)
		VALUES ($1, $2, 'approved', NULLIF($3, ''), $4)`,
		revision.ID, reviewerID, comment, expectedFingerprint,
	); err != nil {
		return Revision{}, fmt.Errorf("insert rule approval: %w", err)
	}
	if _, err := tx.Exec(ctx, "UPDATE site_rule_revisions SET status = 'approved' WHERE id = $1", revision.ID); err != nil {
		return Revision{}, fmt.Errorf("approve rule revision: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ($1, $2, 'site_rule.approve', 'site_rule_revision', $3, $4)`,
		actor.Type, actor.ID, revision.ID, mustJSON(map[string]any{"fingerprint": expectedFingerprint}),
	); err != nil {
		return Revision{}, fmt.Errorf("audit rule approval: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return Revision{}, fmt.Errorf("commit rule approval: %w", err)
	}
	revision.Status = "approved"
	return revision, nil
}

func (s *Store) Activate(ctx context.Context, revisionID string, actor workflow.Actor) (Revision, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Revision{}, fmt.Errorf("begin rule activation transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	revision, err := scanRevision(tx.QueryRow(ctx, revisionSelect+" WHERE sr.id = $1 FOR UPDATE", revisionID))
	if err != nil {
		return Revision{}, err
	}
	if revision.Status != "approved" {
		return Revision{}, fmt.Errorf("%w: only approved rule revisions can be activated", ErrConflict)
	}
	if _, err := tx.Exec(ctx, "UPDATE sites SET active_rule_revision_id = $2, updated_at = now() WHERE id = $1", revision.SiteID, revision.ID); err != nil {
		return Revision{}, fmt.Errorf("activate rule revision: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ($1, NULLIF($2, ''), 'site_rule.activate', 'site', $3, $4)`,
		actor.Type, actor.ID, revision.SiteID, mustJSON(map[string]any{
			"rule_revision_id": revision.ID, "fingerprint": revision.Fingerprint,
		}),
	); err != nil {
		return Revision{}, fmt.Errorf("audit rule activation: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return Revision{}, fmt.Errorf("commit rule activation: %w", err)
	}
	return revision, nil
}

func (s *Store) Active(ctx context.Context, siteCode string) (Revision, error) {
	return scanRevision(s.pool.QueryRow(ctx, revisionSelect+`
		WHERE s.code = $1 AND s.active_rule_revision_id = sr.id`, strings.ToUpper(strings.TrimSpace(siteCode))))
}

func (s *Store) Get(ctx context.Context, revisionID string) (Revision, error) {
	return scanRevision(s.pool.QueryRow(ctx, revisionSelect+" WHERE sr.id = $1", revisionID))
}

func (s *Store) List(ctx context.Context, siteCode string) ([]Revision, error) {
	rows, err := s.pool.Query(ctx, revisionSelect+` WHERE s.code = $1 ORDER BY sr.revision DESC`, strings.ToUpper(strings.TrimSpace(siteCode)))
	if err != nil {
		return nil, fmt.Errorf("list rule revisions: %w", err)
	}
	defer rows.Close()
	revisions := make([]Revision, 0)
	for rows.Next() {
		revision, err := scanRevision(rows)
		if err != nil {
			return nil, err
		}
		revisions = append(revisions, revision)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate rule revisions: %w", err)
	}
	return revisions, nil
}

func (s *Store) ListSites(ctx context.Context) ([]SiteSummary, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT s.id::text, s.code, s.name, s.adapter, s.enabled, s.live_validation_status,
		       COALESCE(s.active_rule_revision_id::text, ''), COALESCE(sr.fingerprint, '')
		FROM sites s
		LEFT JOIN site_rule_revisions sr ON sr.id = s.active_rule_revision_id
		ORDER BY s.code`)
	if err != nil {
		return nil, fmt.Errorf("list sites: %w", err)
	}
	defer rows.Close()
	sites := make([]SiteSummary, 0)
	for rows.Next() {
		var site SiteSummary
		if err := rows.Scan(
			&site.ID, &site.Code, &site.Name, &site.Adapter, &site.Enabled,
			&site.LiveValidationStatus, &site.ActiveRuleRevisionID, &site.ActiveRuleFingerprint,
		); err != nil {
			return nil, fmt.Errorf("scan site: %w", err)
		}
		sites = append(sites, site)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate sites: %w", err)
	}
	return sites, nil
}

func (s *Store) ReadMarkdown(revision Revision) ([]byte, error) {
	cleaned := filepath.Clean(filepath.FromSlash(revision.MarkdownPath))
	if filepath.IsAbs(cleaned) || cleaned == ".." || strings.HasPrefix(cleaned, ".."+string(filepath.Separator)) {
		return nil, fmt.Errorf("invalid stored rule path")
	}
	body, err := os.ReadFile(filepath.Join(s.root, cleaned))
	if err != nil {
		return nil, fmt.Errorf("read rule Markdown: %w", err)
	}
	sum := sha256.Sum256(body)
	if hex.EncodeToString(sum[:]) != revision.MarkdownSHA256 {
		return nil, fmt.Errorf("stored rule Markdown checksum mismatch")
	}
	return body, nil
}

func (s *Store) writeImmutable(relativePath string, raw []byte) error {
	path := filepath.Join(s.root, filepath.FromSlash(relativePath))
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		return fmt.Errorf("create site rule directory: %w", err)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o640)
	if errors.Is(err, os.ErrExist) {
		existing, readErr := os.ReadFile(path)
		if readErr != nil {
			return fmt.Errorf("read existing immutable rule: %w", readErr)
		}
		if !jsonBytesEqual(existing, raw) {
			return fmt.Errorf("immutable rule path collision")
		}
		return nil
	}
	if err != nil {
		return fmt.Errorf("create immutable rule: %w", err)
	}
	defer file.Close()
	if _, err := file.Write(raw); err != nil {
		return fmt.Errorf("write immutable rule: %w", err)
	}
	if err := file.Sync(); err != nil {
		return fmt.Errorf("sync immutable rule: %w", err)
	}
	return nil
}

const revisionSelect = `
	SELECT sr.id::text, sr.site_id::text, s.code, sr.revision, sr.status,
	       sr.fingerprint, COALESCE(sr.source_url, ''), sr.captured_at,
	       sr.markdown_path, sr.markdown_sha256, sr.parsed_policy,
	       sr.obligations, sr.created_at
	FROM site_rule_revisions sr
	JOIN sites s ON s.id = sr.site_id`

func scanRevision(row pgx.Row) (Revision, error) {
	var revision Revision
	err := row.Scan(
		&revision.ID, &revision.SiteID, &revision.SiteCode, &revision.Revision,
		&revision.Status, &revision.Fingerprint, &revision.SourceURL, &revision.CapturedAt,
		&revision.MarkdownPath, &revision.MarkdownSHA256, &revision.Policy,
		&revision.Obligations, &revision.CreatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return Revision{}, ErrNotFound
	}
	if err != nil {
		return Revision{}, fmt.Errorf("scan rule revision: %w", err)
	}
	return revision, nil
}

func parseCapturedAt(value string) time.Time {
	if parsed, err := time.Parse("2006-01-02", value); err == nil {
		return parsed
	}
	parsed, _ := time.Parse(time.RFC3339, value)
	return parsed
}

func nullableUUID(value string) any {
	parsed, err := uuid.Parse(value)
	if err != nil {
		return nil
	}
	return parsed
}

func jsonBytesEqual(left, right []byte) bool {
	leftSum := sha256.Sum256(left)
	rightSum := sha256.Sum256(right)
	return leftSum == rightSum
}

func mustJSON(value any) []byte {
	body, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return body
}
