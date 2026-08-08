package integrations

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestStoreEncryptedIntegrationConfiguration(t *testing.T) {
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
	keyring, _, err := security.LoadOrCreateKeyring(filepath.Join(t.TempDir(), "master-keys"))
	if err != nil {
		t.Fatal(err)
	}
	store := NewStore(pool, security.NewSecretStore(pool, keyring))
	suffix := uuid.NewString()
	nameSuffix := suffix[:8]
	var userID string
	if err := pool.QueryRow(ctx, `
		INSERT INTO users(username, password_hash, role) VALUES ($1, 'integration-only', 'admin')
		RETURNING id::text`, "integration-"+suffix).Scan(&userID); err != nil {
		t.Fatal(err)
	}
	actor := workflow.Actor{Type: "user", ID: userID}
	var secretIDs []string
	t.Cleanup(func() {
		cleanupCtx := context.Background()
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM downloader_path_mappings WHERE downloader_id IN (SELECT id FROM downloaders WHERE name = $1)", "qbit-"+nameSuffix)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM downloaders WHERE name = $1", "qbit-"+nameSuffix)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM image_hosts WHERE name = $1", "imgbb-"+nameSuffix)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM screenshot_profiles WHERE name = $1", "default-"+nameSuffix)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM site_credentials WHERE name = $1", "cookie-"+nameSuffix)
		for _, secretID := range secretIDs {
			_, _ = pool.Exec(cleanupCtx, "DELETE FROM secrets WHERE id = $1", secretID)
		}
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM users WHERE id = $1", userID)
	})

	credential, err := store.PutSiteCredential(ctx, "U2", "cookie-"+nameSuffix, "uid=encrypted-cookie", actor)
	if err != nil || !credential.Enabled {
		t.Fatalf("PutSiteCredential() credential/error = %#v/%v", credential, err)
	}
	credentials, err := store.ListSiteCredentials(ctx, "U2")
	if err != nil {
		t.Fatal(err)
	}
	foundCredential := false
	for _, item := range credentials {
		if item.ID == credential.ID {
			foundCredential = true
		}
	}
	if !foundCredential {
		t.Fatal("site credential was not listed")
	}
	runtimeSite, err := store.GetRuntimeSite(ctx, "U2")
	if err != nil || runtimeSite.Adapter != "nexusphp" || runtimeSite.Credentials["cookie-"+nameSuffix] != "uid=encrypted-cookie" {
		t.Fatalf("GetRuntimeSite() runtime/error = %#v/%v", runtimeSite, err)
	}

	enabled := true
	downloader, err := store.UpsertDownloader(ctx, "qbit-"+nameSuffix, DownloaderInput{
		Adapter: "qbittorrent", Enabled: &enabled,
		Config:       EndpointConfig{Endpoint: "http://host.docker.internal:8080", Options: map[string]any{"category": "retorrent"}},
		Credentials:  map[string]string{"username": "operator", "password": "encrypted-password"},
		PathMappings: []PathMapping{{RemotePath: "/remote/downloads", LocalPath: "/downloads", Priority: 100}},
	}, actor)
	if err != nil {
		t.Fatalf("UpsertDownloader() error = %v", err)
	}
	if len(downloader.CredentialFields) != 2 || len(downloader.PathMappings) != 1 {
		t.Fatalf("downloader credential fields/mappings = %#v/%#v", downloader.CredentialFields, downloader.PathMappings)
	}
	runtimeDownloader, err := store.GetRuntimeDownloader(ctx, downloader.Name)
	if err != nil {
		t.Fatalf("GetRuntimeDownloader() error = %v", err)
	}
	if runtimeDownloader.Credentials["password"] != "encrypted-password" || runtimeDownloader.EndpointConfig.Endpoint != "http://host.docker.internal:8080" {
		t.Fatalf("runtime downloader did not decrypt expected configuration")
	}
	if err := store.RecordDownloaderHealth(ctx, downloader.Name, "ready", map[string]any{"webapi_version": "test"}, actor); err != nil {
		t.Fatal(err)
	}
	if err := store.AuditDownloaderAction(ctx, downloader.Name, "torrent.inspect", map[string]any{"hash": "test"}, actor); err != nil {
		t.Fatal(err)
	}
	downloaderList, err := store.ListDownloaders(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !containsDownloader(downloaderList, downloader.ID) {
		t.Fatal("downloader was not listed")
	}
	for _, item := range downloaderList {
		if item.ID == downloader.ID && item.HealthStatus != "ready" {
			t.Fatalf("downloader health status = %s", item.HealthStatus)
		}
	}

	imageHost, err := store.UpsertImageHost(ctx, "imgbb-"+nameSuffix, ImageHostInput{
		Adapter: "imgbb", Enabled: &enabled, Priority: 10,
		Config:      EndpointConfig{Endpoint: "https://api.imgbb.com/1/upload"},
		Credentials: map[string]string{"api_key": "encrypted-image-key"},
	}, actor)
	if err != nil || len(imageHost.CredentialFields) != 1 {
		t.Fatalf("UpsertImageHost() imageHost/error = %#v/%v", imageHost, err)
	}

	first, err := store.CreateScreenshotProfile(ctx, ScreenshotProfileInput{
		Name: "default-" + nameSuffix, Enabled: &enabled,
		Config: map[string]any{"count": 6, "format": "png", "comparison": false},
	}, actor)
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.CreateScreenshotProfile(ctx, ScreenshotProfileInput{
		Name: "default-" + nameSuffix, Enabled: &enabled,
		Config: map[string]any{"count": 8, "format": "webp", "comparison": true},
	}, actor)
	if err != nil || second.Revision != first.Revision+1 {
		t.Fatalf("screenshot revisions = %d/%d error=%v", first.Revision, second.Revision, err)
	}
	runtimeScreenshot, err := store.GetRuntimeScreenshotProfile(ctx, "default-"+nameSuffix)
	if err != nil || runtimeScreenshot.Revision != second.Revision || runtimeScreenshot.ScreenshotConfig.Count != 8 || runtimeScreenshot.ScreenshotConfig.Format != "webp" {
		t.Fatalf("runtime screenshot profile/error = %#v/%v", runtimeScreenshot, err)
	}

	rows, err := pool.Query(ctx, `
		SELECT secret_id::text FROM site_credentials WHERE id = $1
		UNION ALL SELECT secret_id::text FROM downloaders WHERE id = $2
		UNION ALL SELECT secret_id::text FROM image_hosts WHERE id = $3`, credential.ID, downloader.ID, imageHost.ID)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var secretID string
		if err := rows.Scan(&secretID); err != nil {
			t.Fatal(err)
		}
		secretIDs = append(secretIDs, secretID)
	}
	rows.Close()
	if len(secretIDs) != 3 {
		t.Fatalf("encrypted secret count = %d, want 3", len(secretIDs))
	}
}

func containsDownloader(items []Downloader, id string) bool {
	for _, item := range items {
		if item.ID == id {
			return true
		}
	}
	return false
}
