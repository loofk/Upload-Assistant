package rules

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type SiteInput struct {
	Name    string   `json:"name"`
	Adapter string   `json:"adapter"`
	Enabled bool     `json:"enabled"`
	Aliases []string `json:"aliases"`
	Tags    []string `json:"tags"`
}

func (s *Store) UpsertSite(ctx context.Context, code string, input SiteInput, actor workflow.Actor) (SiteSummary, error) {
	code = strings.ToUpper(strings.TrimSpace(code))
	input.Name = strings.TrimSpace(input.Name)
	input.Adapter = strings.ToLower(strings.TrimSpace(input.Adapter))
	if !siteCodePattern.MatchString(code) {
		return SiteSummary{}, fmt.Errorf("invalid site code %q", code)
	}
	if input.Name == "" || utf8.RuneCountInString(input.Name) > 100 {
		return SiteSummary{}, fmt.Errorf("site name is required and must not exceed 100 characters")
	}
	if !validSiteAdapter(input.Adapter) {
		return SiteSummary{}, fmt.Errorf("unsupported site adapter %q", input.Adapter)
	}
	aliases, err := normalizedLabels(input.Aliases, 64)
	if err != nil {
		return SiteSummary{}, fmt.Errorf("invalid aliases: %w", err)
	}
	tags, err := normalizedLabels(input.Tags, 32)
	if err != nil {
		return SiteSummary{}, fmt.Errorf("invalid tags: %w", err)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return SiteSummary{}, fmt.Errorf("begin site transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var siteID, currentAdapter string
	err = tx.QueryRow(ctx, "SELECT id::text, adapter FROM sites WHERE code = $1 FOR UPDATE", code).Scan(&siteID, &currentAdapter)
	created := false
	if errors.Is(err, pgx.ErrNoRows) {
		err = tx.QueryRow(ctx, `INSERT INTO sites(code, name, adapter, enabled) VALUES ($1,$2,$3,$4) RETURNING id::text`, code, input.Name, input.Adapter, input.Enabled).Scan(&siteID)
		created = true
	} else if err == nil {
		if currentAdapter != input.Adapter {
			return SiteSummary{}, fmt.Errorf("%w: adapter is immutable after site creation", ErrConflict)
		}
		_, err = tx.Exec(ctx, "UPDATE sites SET name=$2, enabled=$3, updated_at=now() WHERE id=$1", siteID, input.Name, input.Enabled)
	}
	if err != nil {
		return SiteSummary{}, fmt.Errorf("save site: %w", err)
	}
	if _, err = tx.Exec(ctx, "DELETE FROM site_aliases WHERE site_id=$1", siteID); err != nil {
		return SiteSummary{}, fmt.Errorf("replace aliases: %w", err)
	}
	if _, err = tx.Exec(ctx, "DELETE FROM site_tags WHERE site_id=$1", siteID); err != nil {
		return SiteSummary{}, fmt.Errorf("replace tags: %w", err)
	}
	for _, alias := range aliases {
		normalized := strings.ToLower(alias)
		var collision bool
		if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM sites WHERE lower(code)= $1 AND id::text <> $2)", normalized, siteID).Scan(&collision); err != nil {
			return SiteSummary{}, fmt.Errorf("check alias collision: %w", err)
		}
		if collision {
			return SiteSummary{}, fmt.Errorf("%w: alias %q conflicts with a canonical site code", ErrConflict, alias)
		}
		if _, err := tx.Exec(ctx, "INSERT INTO site_aliases(site_id, alias, normalized_alias, created_by) VALUES ($1,$2,$3,$4)", siteID, alias, normalized, nullableUUID(actor.ID)); err != nil {
			return SiteSummary{}, fmt.Errorf("save alias %q: %w", alias, err)
		}
	}
	for _, tag := range tags {
		if _, err := tx.Exec(ctx, "INSERT INTO site_tags(site_id, tag, normalized_tag, created_by) VALUES ($1,$2,$3,$4)", siteID, tag, strings.ToLower(tag), nullableUUID(actor.ID)); err != nil {
			return SiteSummary{}, fmt.Errorf("save tag %q: %w", tag, err)
		}
	}
	if _, err := tx.Exec(ctx, `INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ($1,NULLIF($2,''),'site.upsert','site',$3,$4)`, actor.Type, actor.ID, siteID, mustJSON(map[string]any{"code": code, "created": created, "name": input.Name, "adapter": input.Adapter, "enabled": input.Enabled, "aliases": aliases, "tags": tags})); err != nil {
		return SiteSummary{}, fmt.Errorf("audit site upsert: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return SiteSummary{}, fmt.Errorf("commit site transaction: %w", err)
	}
	sites, err := s.ListSites(ctx)
	if err != nil {
		return SiteSummary{}, err
	}
	for _, site := range sites {
		if site.Code == code {
			return site, nil
		}
	}
	return SiteSummary{}, ErrNotFound
}

func validSiteAdapter(value string) bool {
	switch value {
	case "config_only", "nexusphp", "mteam_api", "ttg":
		return true
	}
	return false
}

func normalizedLabels(values []string, maxRunes int) ([]string, error) {
	seen := map[string]bool{}
	result := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if utf8.RuneCountInString(value) > maxRunes {
			return nil, fmt.Errorf("%q exceeds %d characters", value, maxRunes)
		}
		key := strings.ToLower(value)
		if !seen[key] {
			seen[key] = true
			result = append(result, value)
		}
	}
	sort.Slice(result, func(i, j int) bool { return strings.ToLower(result[i]) < strings.ToLower(result[j]) })
	return result, nil
}
