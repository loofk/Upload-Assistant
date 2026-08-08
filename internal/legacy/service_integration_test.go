package legacy

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestServiceImportsIdempotentlyAndExpiresEncryptedArchive(t *testing.T) {
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
	keyring, _, err := security.LoadOrCreateKeyring(filepath.Join(t.TempDir(), "master-keys"))
	if err != nil {
		t.Fatal(err)
	}
	secretStore := security.NewSecretStore(pool, keyring)
	integrationStore := integrations.NewStore(pool, secretStore)
	root := t.TempDir()
	suffix := strings.ReplaceAll(uuid.NewString()[:8], "-", "")
	downloaderName := "legacy-box-" + suffix
	writeLegacyFixture(t, root, `config = {
  "DEFAULT": {"default_torrent_client": "`+downloaderName+`", "screens": "4"},
  "TORRENT_CLIENTS": {"`+downloaderName+`": {
    "torrent_client": "qbit", "qbit_url": "https://qb.example.test", "qbit_port": "443",
    "qbit_user": "operator", "qbit_pass": "migration-private-password",
    "local_path": ["/downloads"], "remote_path": ["/srv/downloads"]
  }}
}`)
	service, err := NewService(pool, secretStore, integrationStore, root, nil)
	if err != nil {
		t.Fatal(err)
	}
	var userID string
	if err := pool.QueryRow(ctx, `
		INSERT INTO users(username, password_hash, role) VALUES ($1, 'integration-only', 'admin')
		RETURNING id::text`, "legacy-"+uuid.NewString()).Scan(&userID); err != nil {
		t.Fatal(err)
	}
	actor := workflow.Actor{Type: "user", ID: userID}
	t.Cleanup(func() {
		cleanupCtx := context.Background()
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM audit_events WHERE actor_id IN ($1, 'legacy-retention')", userID)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM legacy_imports WHERE imported_by = $1", userID)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM downloader_path_mappings WHERE downloader_id IN (SELECT id FROM downloaders WHERE name = $1)", downloaderName)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM downloaders WHERE name = $1", downloaderName)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM screenshot_profiles WHERE name = 'legacy-default' AND created_by = $1", userID)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM secrets WHERE created_by = $1", userID)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM users WHERE id = $1", userID)
	})

	preview, err := service.Preview(ctx)
	if err != nil || !preview.OK {
		t.Fatalf("Preview() preview/error = %#v/%v", preview, err)
	}
	record, err := service.Import(ctx, ImportRequest{SourceFingerprint: preview.SourceFingerprint, ConfirmImport: true}, actor)
	if err != nil || !record.OK || record.Status != "complete" || !record.ArchiveAvailable || len(record.ArchiveSHA256) != 64 {
		t.Fatalf("Import() status/error = %s/%v", record.Status, err)
	}
	if len(record.Report.Applied) != 2 || strings.Contains(record.Report.Summary, "migration-private-password") {
		t.Fatalf("unexpected redacted import report")
	}

	var archiveSecretID string
	if err := pool.QueryRow(ctx, "SELECT archive_secret_id::text FROM legacy_imports WHERE id = $1", record.ID).Scan(&archiveSecretID); err != nil {
		t.Fatal(err)
	}
	archive, err := secretStore.Get(ctx, archiveSecretID, archivePurpose(record.ID))
	var archived archiveDocument
	if err != nil || json.Unmarshal(archive, &archived) != nil || len(archived.Files) != 1 || !strings.Contains(string(archived.Files[0].Content), "migration-private-password") {
		t.Fatal("encrypted archive could not be recovered internally")
	}
	var publicReport string
	if err := pool.QueryRow(ctx, "SELECT report::text FROM legacy_imports WHERE id = $1", record.ID).Scan(&publicReport); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(publicReport, "migration-private-password") {
		t.Fatal("database migration report contains plaintext credential")
	}

	repeated, err := service.Import(ctx, ImportRequest{SourceFingerprint: preview.SourceFingerprint, ConfirmImport: true}, actor)
	if err != nil || repeated.ID != record.ID {
		t.Fatalf("idempotent Import() id/error = %s/%v", repeated.ID, err)
	}
	var profileCount int
	if err := pool.QueryRow(ctx, "SELECT count(*) FROM screenshot_profiles WHERE name = 'legacy-default' AND created_by = $1", userID).Scan(&profileCount); err != nil || profileCount != 1 {
		t.Fatalf("screenshot profile count/error = %d/%v", profileCount, err)
	}

	if _, err := pool.Exec(ctx, "UPDATE legacy_imports SET expires_at = now() - interval '1 second' WHERE id = $1", record.ID); err != nil {
		t.Fatal(err)
	}
	deleted, err := service.CleanupExpired(ctx, workflow.Actor{Type: "system", ID: "legacy-retention"})
	if err != nil || deleted != 1 {
		t.Fatalf("CleanupExpired() count/error = %d/%v", deleted, err)
	}
	expired, err := service.Get(ctx, record.ID)
	if err != nil || expired.ArchiveAvailable || expired.ArchiveDeletedAt == nil {
		t.Fatalf("expired archive record/error = %#v/%v", expired, err)
	}
	if _, err := secretStore.Get(ctx, archiveSecretID, archivePurpose(record.ID)); err == nil {
		t.Fatal("expired archive secret still exists")
	}
}
