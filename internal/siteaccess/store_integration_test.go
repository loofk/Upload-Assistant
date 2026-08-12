package siteaccess

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestStoreFailsClosedAndPersistsRateLimitDecisions(t *testing.T) {
	databaseURL := os.Getenv("UA_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("UA_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}

	code := "ACCESS_" + strings.ToUpper(strings.ReplaceAll(uuid.NewString()[:12], "-", ""))
	var siteID string
	if err := pool.QueryRow(ctx, `INSERT INTO sites(code,name,adapter,enabled) VALUES ($1,'Access fixture','nexusphp',true) RETURNING id::text`, code).Scan(&siteID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), `DELETE FROM sites WHERE id=$1`, siteID) })

	store := NewStore(pool)
	operator := PolicyInput{
		Enabled: true, GeneralMinIntervalSeconds: 2, GeneralMaxRequestsPerHour: 10,
		SearchMinIntervalSeconds: 3, SearchMaxRequestsPerHour: 8, MaxConcurrency: 2,
	}
	if _, err := store.UpsertPolicy(ctx, code, operator, workflow.Actor{Type: "test", ID: "site-access"}); err != nil {
		t.Fatal(err)
	}
	_, err = store.Acquire(ctx, sites.AccessRequest{SiteCode: code, Operation: "fixture.before_rule", Class: sites.AccessGeneral})
	var denied *DeniedError
	if !errors.As(err, &denied) || denied.Code != "site_access_rule_required" {
		t.Fatalf("Acquire() before approved v2 rule = %T/%v", err, err)
	}

	policyJSON, err := json.Marshal(rules.Policy{
		SchemaVersion: 2,
		Site:          rules.Site{Code: code, DisplayName: "Access fixture", Roles: []string{"source"}},
		Source:        rules.Source{URL: "https://tracker.invalid/rules", CapturedAt: "2026-08-10", Complete: true, Scope: "all"},
		Access: rules.Access{
			ServiceAccess: "allowed", SearchAccess: "allowed",
			GeneralMinIntervalSeconds: 5, GeneralMaxRequestsPerHour: 3,
			SearchMinIntervalSeconds: 7, SearchMaxRequestsPerHour: 2, MaxConcurrency: 1,
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	var revisionID string
	if err := pool.QueryRow(ctx, `INSERT INTO site_rule_revisions(
		site_id,revision,status,fingerprint,source_url,captured_at,markdown_path,markdown_sha256,parsed_policy
	) VALUES ($1,1,'approved',$2,'https://tracker.invalid/rules',now(),'fixture.md',$3,$4) RETURNING id::text`,
		siteID, strings.Repeat("a", 64), strings.Repeat("b", 64), policyJSON).Scan(&revisionID); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE sites SET active_rule_revision_id=$2 WHERE id=$1`, siteID, revisionID); err != nil {
		t.Fatal(err)
	}

	effective, err := store.GetPolicy(ctx, code)
	if err != nil {
		t.Fatal(err)
	}
	if len(effective.Blockers) != 0 || effective.GeneralMinIntervalSeconds != 5 || effective.GeneralMaxRequestsPerHour != 3 ||
		effective.SearchMinIntervalSeconds != 7 || effective.SearchMaxRequestsPerHour != 2 || effective.MaxConcurrency != 1 {
		t.Fatalf("effective policy = %#v", effective)
	}

	lease, err := store.Acquire(ctx, sites.AccessRequest{SiteCode: code, Operation: "fixture.search", Class: sites.AccessSearch})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Complete(ctx, lease, sites.AccessResult{Outcome: "cooldown", StatusCode: 429, RetryAfter: time.Hour}); err != nil {
		t.Fatal(err)
	}
	_, err = store.Acquire(ctx, sites.AccessRequest{SiteCode: code, Operation: "fixture.search_again", Class: sites.AccessSearch})
	var deferred *DeferredError
	if !errors.As(err, &deferred) || deferred.Reason != "remote_cooldown" || time.Until(deferred.NotBefore) < 50*time.Minute {
		t.Fatalf("Acquire() after 429 = %T/%v", err, err)
	}
	var completedEvents int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM audit_events WHERE resource_id=$1 AND action='site_access.completed'`, siteID).Scan(&completedEvents); err != nil || completedEvents != 1 {
		t.Fatalf("completed audit count/error = %d/%v", completedEvents, err)
	}
}
