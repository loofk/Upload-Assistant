package rules

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestStoreImportApprovalAndActivation(t *testing.T) {
	databaseURL := os.Getenv("UA_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("UA_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, databaseURL)
	if err != nil {
		t.Fatalf("database.Open() error = %v", err)
	}
	t.Cleanup(pool.Close)
	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatalf("database.Migrate() error = %v", err)
	}
	store, err := NewStore(pool, t.TempDir())
	if err != nil {
		t.Fatalf("NewStore() error = %v", err)
	}
	actor := workflow.Actor{Type: "test", ID: "rule-import"}
	reviewerID := ""
	draft, err := store.Import(ctx, []byte(testRuleMarkdown(false)), actor)
	if err != nil {
		t.Fatalf("Import() draft error = %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), "UPDATE sites SET active_rule_revision_id = NULL WHERE code = 'U2'")
		_, _ = pool.Exec(context.Background(), "DELETE FROM site_rule_revisions WHERE site_id = $1", draft.SiteID)
		if reviewerID != "" {
			_, _ = pool.Exec(context.Background(), "DELETE FROM users WHERE id = $1", reviewerID)
		}
	})
	again, err := store.Import(ctx, []byte(testRuleMarkdown(false)), actor)
	if err != nil || again.ID != draft.ID {
		t.Fatalf("idempotent Import() revision/error = %s/%v, want %s", again.ID, err, draft.ID)
	}
	markdown, err := store.ReadMarkdown(draft)
	if err != nil || string(markdown) != testRuleMarkdown(false) {
		t.Fatalf("ReadMarkdown() mismatch/error = %v", err)
	}
	if _, err := store.Approve(ctx, draft.ID, draft.Fingerprint, "reviewed", workflow.Actor{Type: "user", ID: uuid.NewString()}); !errors.Is(err, ErrSourceIncomplete) {
		t.Fatalf("Approve() incomplete error = %v, want ErrSourceIncomplete", err)
	}

	reviewerID = uuid.NewString()
	if _, err := pool.Exec(ctx, `
		INSERT INTO users(id, username, password_hash, role)
		VALUES ($1, $2, 'integration-test-not-a-real-password-hash', 'admin')`, reviewerID, "rule-reviewer-"+reviewerID); err != nil {
		t.Fatalf("insert reviewer: %v", err)
	}
	complete, err := store.Import(ctx, []byte(testRuleMarkdown(true)), actor)
	if err != nil {
		t.Fatalf("Import() complete error = %v", err)
	}
	retiredDraft, err := store.Get(ctx, draft.ID)
	if err != nil || retiredDraft.Status != "retired" {
		t.Fatalf("superseded draft status/error = %s/%v, want retired", retiredDraft.Status, err)
	}
	for _, section := range reviewSectionOrder {
		if _, err := store.SetReviewCheck(ctx, complete.ID, section, complete.Fingerprint, "confirmed", "reviewed", workflow.Actor{Type: "user", ID: reviewerID}); err != nil {
			t.Fatalf("SetReviewCheck(%s) error = %v", section, err)
		}
	}
	approved, err := store.Approve(ctx, complete.ID, complete.Fingerprint, "verified against supplied source", workflow.Actor{Type: "user", ID: reviewerID})
	if err != nil || approved.Status != "approved" {
		t.Fatalf("Approve() revision/error = %s/%v", approved.Status, err)
	}
	active, err := store.Activate(ctx, approved.ID, workflow.Actor{Type: "user", ID: reviewerID})
	if err != nil {
		t.Fatalf("Activate() error = %v", err)
	}
	loaded, err := store.Active(ctx, "U2")
	if err != nil || loaded.ID != active.ID {
		t.Fatalf("Active() revision/error = %s/%v, want %s", loaded.ID, err, active.ID)
	}
	corrected, err := store.CorrectHardGate(ctx, approved.ID, approved.Fingerprint, "upload_limit", json.RawMessage(`{"upload":"100MB/s"}`), "原文明确要求全局上传限速", workflow.Actor{Type: "user", ID: reviewerID})
	if err != nil {
		t.Fatalf("CorrectHardGate() error = %v", err)
	}
	correctedPolicy, err := ParsePolicy(corrected.Policy)
	if err != nil || corrected.Status != "draft" || corrected.Revision <= approved.Revision || correctedPolicy.Limits.Upload != "100MB/s" {
		t.Fatalf("corrected revision/policy/error = %#v/%#v/%v", corrected, correctedPolicy, err)
	}
	correctedReview, err := store.GetReview(ctx, corrected.ID)
	if err != nil || correctedReview.ConfirmedCount != 0 || correctedReview.ApprovalReady {
		t.Fatalf("corrected review/error = %#v/%v", correctedReview, err)
	}
	discarded, err := store.DiscardDraft(ctx, corrected.ID, corrected.Fingerprint, workflow.Actor{Type: "user", ID: reviewerID})
	if err != nil || discarded.Status != "retired" {
		t.Fatalf("DiscardDraft() revision/error = %s/%v, want retired", discarded.Status, err)
	}
	revisions, err := store.List(ctx, "U2")
	if err != nil || len(revisions) < 2 {
		t.Fatalf("List() count/error = %d/%v", len(revisions), err)
	}
}
