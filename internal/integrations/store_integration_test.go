package integrations

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
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
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM downloader_path_mappings WHERE downloader_id IN (SELECT id FROM downloaders WHERE name = ANY($1))", []string{"qbit-" + nameSuffix, "transmission-" + nameSuffix, "rtorrent-" + nameSuffix, "switch-" + nameSuffix, "deluge-" + nameSuffix})
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM downloaders WHERE name = ANY($1)", []string{"qbit-" + nameSuffix, "transmission-" + nameSuffix, "rtorrent-" + nameSuffix, "switch-" + nameSuffix, "deluge-" + nameSuffix})
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM image_hosts WHERE name = $1", "imgbb-"+nameSuffix)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM notification_channels WHERE name = $1", "discord-"+nameSuffix)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM media_managers WHERE name = $1", "sonarr-"+nameSuffix)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM metadata_providers WHERE name = $1", "tmdb-"+nameSuffix)
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
	if err != nil || runtimeSite.Adapter != "nexusphp" || runtimeSite.Credentials["cookie-"+nameSuffix] != "uid=encrypted-cookie" ||
		runtimeSite.ID == "" || len(runtimeSite.ConfigurationSHA256) != 64 {
		t.Fatalf("GetRuntimeSite() runtime/error = %#v/%v", runtimeSite, err)
	}
	var retiredSiteSecretID string
	if err := pool.QueryRow(ctx, "SELECT secret_id::text FROM site_credentials WHERE id = $1", credential.ID).Scan(&retiredSiteSecretID); err != nil {
		t.Fatal(err)
	}
	secretIDs = append(secretIDs, retiredSiteSecretID)
	rotatedCredential, err := store.PutSiteCredential(ctx, "U2", "cookie-"+nameSuffix, "uid=rotated-cookie", actor)
	if err != nil || rotatedCredential.ID != credential.ID {
		t.Fatalf("rotated PutSiteCredential() credential/error = %#v/%v", rotatedCredential, err)
	}
	rotatedRuntimeSite, err := store.GetRuntimeSite(ctx, "U2")
	if err != nil || rotatedRuntimeSite.Credentials["cookie-"+nameSuffix] != "uid=rotated-cookie" ||
		rotatedRuntimeSite.ConfigurationSHA256 == runtimeSite.ConfigurationSHA256 {
		t.Fatalf("rotated GetRuntimeSite() runtime/error = %#v/%v", rotatedRuntimeSite, err)
	}
	if err := store.AuditSiteAction(ctx, "U2", "source.inspect", map[string]any{"torrent_id": "fixture"}, actor); err != nil {
		t.Fatal(err)
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
	if len(downloader.CredentialFields) != 2 || len(downloader.PathMappings) != 1 || !downloader.AdapterCapability.RuntimeSupported {
		t.Fatalf("downloader credential fields/mappings = %#v/%#v", downloader.CredentialFields, downloader.PathMappings)
	}
	runtimeDownloader, err := store.GetRuntimeDownloader(ctx, downloader.Name)
	if err != nil {
		t.Fatalf("GetRuntimeDownloader() error = %v", err)
	}
	if runtimeDownloader.Credentials["password"] != "encrypted-password" || runtimeDownloader.EndpointConfig.Endpoint != "http://host.docker.internal:8080" ||
		len(runtimeDownloader.ConfigurationSHA256) != 64 {
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

	transmission, err := store.UpsertDownloader(ctx, "transmission-"+nameSuffix, DownloaderInput{
		Adapter: "transmission", Enabled: &enabled,
		Config: EndpointConfig{Endpoint: "http://host.docker.internal:9091/transmission/rpc"},
	}, actor)
	if err != nil || !transmission.Enabled || !transmission.AdapterCapability.RuntimeSupported || transmission.AdapterCapability.Operations.SkipChecking {
		t.Fatalf("Transmission downloader/error = %#v/%v", transmission, err)
	}
	rtorrent, err := store.UpsertDownloader(ctx, "rtorrent-"+nameSuffix, DownloaderInput{
		Adapter: "rtorrent", Enabled: &enabled,
		Config: EndpointConfig{Endpoint: "http://host.docker.internal/RPC2"},
	}, actor)
	if err != nil || !rtorrent.Enabled || !rtorrent.AdapterCapability.RuntimeSupported || rtorrent.AdapterCapability.Operations.SkipChecking || len(rtorrent.AdapterCapability.Constraints) == 0 {
		t.Fatalf("rTorrent downloader/error = %#v/%v", rtorrent, err)
	}
	switchName := "switch-" + nameSuffix
	if _, err := store.UpsertDownloader(ctx, switchName, DownloaderInput{
		Adapter: "qbittorrent", Enabled: &enabled,
		Config:      EndpointConfig{Endpoint: "http://host.docker.internal:8080"},
		Credentials: map[string]string{"api_key": "must-not-cross-adapters"},
	}, actor); err != nil {
		t.Fatal(err)
	}
	if _, err := store.UpsertDownloader(ctx, switchName, DownloaderInput{
		Adapter: "rtorrent", Enabled: &enabled,
		Config: EndpointConfig{Endpoint: "http://host.docker.internal/RPC2"},
	}, actor); err != nil {
		t.Fatal(err)
	}
	switchedRuntime, err := store.GetRuntimeDownloader(ctx, switchName)
	if err != nil || len(switchedRuntime.Credentials) != 0 || len(switchedRuntime.CredentialFields) != 0 {
		t.Fatalf("switched downloader retained incompatible credentials: fields=%#v error=%v", switchedRuntime.CredentialFields, err)
	}
	if _, err := store.UpsertDownloader(ctx, "deluge-"+nameSuffix, DownloaderInput{
		Adapter: "deluge", Enabled: &enabled,
		Config: EndpointConfig{Endpoint: "http://host.docker.internal:8112/json"},
	}, actor); !errors.Is(err, ErrValidation) {
		t.Fatalf("UpsertDownloader() accepted enabled Deluge without Web password: %v", err)
	}
	deluge, err := store.UpsertDownloader(ctx, "deluge-"+nameSuffix, DownloaderInput{
		Adapter: "deluge", Enabled: &enabled,
		Config:      EndpointConfig{Endpoint: "http://host.docker.internal:8112/json"},
		Credentials: map[string]string{"password": "encrypted-web-password"},
	}, actor)
	if err != nil || !deluge.Enabled || !deluge.AdapterCapability.RuntimeSupported || deluge.AdapterCapability.Operations.Category || deluge.AdapterCapability.Operations.Tags {
		t.Fatalf("Deluge downloader/error = %#v/%v", deluge, err)
	}
	if _, err := store.UpsertDownloader(ctx, "deluge-"+nameSuffix, DownloaderInput{
		Adapter: "deluge", Enabled: &enabled,
		Config: EndpointConfig{Endpoint: "http://host.docker.internal:8112/json"},
	}, actor); err != nil {
		t.Fatalf("UpsertDownloader() did not preserve an existing Deluge Web password: %v", err)
	}
	delugeRuntime, err := store.GetRuntimeDownloader(ctx, "deluge-"+nameSuffix)
	if err != nil || delugeRuntime.Credentials["password"] != "encrypted-web-password" || len(delugeRuntime.CredentialFields) != 1 {
		t.Fatalf("GetRuntimeDownloader() Deluge credentials/error = %#v/%v", delugeRuntime.CredentialFields, err)
	}
	if _, err := store.UpsertDownloader(ctx, "deluge-"+nameSuffix, DownloaderInput{
		Adapter: "deluge", Enabled: &enabled,
		Config:      EndpointConfig{Endpoint: "http://host.docker.internal:8112"},
		Credentials: map[string]string{"username": "native-daemon-user", "password": "must-not-be-stored"},
	}, actor); !errors.Is(err, ErrValidation) {
		t.Fatalf("UpsertDownloader() accepted Deluge native RPC credentials: %v", err)
	}

	imageHost, err := store.UpsertImageHost(ctx, "imgbb-"+nameSuffix, ImageHostInput{
		Adapter: "imgbb", Enabled: &enabled, Priority: 10,
		Config:      EndpointConfig{Endpoint: "https://api.imgbb.com/1/upload"},
		Credentials: map[string]string{"api_key": "encrypted-image-key"},
	}, actor)
	if err != nil || len(imageHost.CredentialFields) != 1 {
		t.Fatalf("UpsertImageHost() imageHost/error = %#v/%v", imageHost, err)
	}
	runtimeImageHost, err := store.GetRuntimeImageHost(ctx, "imgbb-"+nameSuffix)
	if err != nil || runtimeImageHost.Credentials["api_key"] != "encrypted-image-key" || runtimeImageHost.EndpointConfig.Endpoint != "https://api.imgbb.com/1/upload" {
		t.Fatalf("GetRuntimeImageHost() runtime/error = %#v/%v", runtimeImageHost, err)
	}
	if _, err := store.UpsertImageHost(ctx, imageHost.Name, ImageHostInput{
		Adapter: "imgbb", Enabled: &enabled, Priority: 15,
		Config: EndpointConfig{Endpoint: "https://api.imgbb.com/1/upload"},
	}, actor); err != nil {
		t.Fatalf("UpsertImageHost() did not preserve an existing ImgBB key: %v", err)
	}
	runtimeImageHost, err = store.GetRuntimeImageHost(ctx, imageHost.Name)
	if err != nil || runtimeImageHost.Credentials["api_key"] != "encrypted-image-key" {
		t.Fatalf("GetRuntimeImageHost() lost preserved ImgBB key: %#v/%v", runtimeImageHost, err)
	}
	if err := store.RecordImageHostHealth(ctx, imageHost.Name, "ready", map[string]any{"adapter": "imgbb"}, actor); err != nil {
		t.Fatal(err)
	}
	afterHealth, err := store.GetRuntimeImageHost(ctx, imageHost.Name)
	if err != nil || !afterHealth.UpdatedAt.Equal(runtimeImageHost.UpdatedAt) {
		t.Fatalf("image-host health changed configuration revision: before=%s after=%s error=%v", runtimeImageHost.UpdatedAt, afterHealth.UpdatedAt, err)
	}
	if err := store.AuditImageHostAction(ctx, imageHost.Name, "image.upload", map[string]any{"source_sha256": "fixture"}, actor); err != nil {
		t.Fatal(err)
	}
	pixhost, err := store.UpsertImageHost(ctx, "pixhost-"+nameSuffix, ImageHostInput{
		Adapter: "pixhost", Enabled: &enabled, Priority: 20,
		Config: EndpointConfig{Endpoint: "https://api.pixhost.to/images"},
	}, actor)
	if err != nil || len(pixhost.CredentialFields) != 0 {
		t.Fatalf("UpsertImageHost() keyless pixhost/error = %#v/%v", pixhost, err)
	}
	pixhostRuntime, err := store.GetRuntimeImageHost(ctx, pixhost.Name)
	if err != nil || len(pixhostRuntime.Credentials) != 0 || pixhostRuntime.Adapter != "pixhost" {
		t.Fatalf("GetRuntimeImageHost() keyless pixhost/error = %#v/%v", pixhostRuntime, err)
	}
	if _, err := store.UpsertImageHost(ctx, "pixhost-secret-"+nameSuffix, ImageHostInput{
		Adapter: "pixhost", Enabled: &enabled,
		Config:      EndpointConfig{Endpoint: "https://api.pixhost.to/images"},
		Credentials: map[string]string{"api_key": "must-not-be-stored"},
	}, actor); !errors.Is(err, ErrValidation) {
		t.Fatalf("UpsertImageHost() accepted credentials for Pixhost: %v", err)
	}

	notificationChannel, err := store.UpsertNotificationChannel(ctx, "discord-"+nameSuffix, NotificationChannelInput{
		Adapter: "discord_webhook", Enabled: &enabled,
		Config:      NotificationChannelConfig{TimeoutSeconds: 15},
		Credentials: map[string]string{"webhook_url": "https://discord.com/api/webhooks/123456/encrypted-token"},
	}, actor)
	if err != nil || len(notificationChannel.CredentialFields) != 1 {
		t.Fatalf("UpsertNotificationChannel() channel/error = %#v/%v", notificationChannel, err)
	}
	runtimeChannel, err := store.GetRuntimeNotificationChannel(ctx, notificationChannel.Name)
	if err != nil || runtimeChannel.Credentials["webhook_url"] != "https://discord.com/api/webhooks/123456/encrypted-token" || len(runtimeChannel.ConfigurationSHA256) != 64 {
		t.Fatalf("GetRuntimeNotificationChannel() runtime/error = %#v/%v", runtimeChannel, err)
	}
	channels, err := store.ListNotificationChannels(ctx)
	if err != nil || !slices.ContainsFunc(channels, func(item NotificationChannel) bool {
		return item.ID == notificationChannel.ID && len(item.CredentialFields) == 1
	}) {
		t.Fatalf("ListNotificationChannels() = %#v/%v", channels, err)
	}

	mediaManager, err := store.UpsertMediaManager(ctx, "sonarr-"+nameSuffix, MediaManagerInput{
		Adapter: "sonarr", Enabled: &enabled,
		Config:      EndpointConfig{Endpoint: "http://host.docker.internal:8989"},
		Credentials: map[string]string{"api_key": "encrypted-sonarr-key"},
	}, actor)
	if err != nil || len(mediaManager.CredentialFields) != 1 {
		t.Fatalf("UpsertMediaManager() manager/error = %#v/%v", mediaManager, err)
	}
	runtimeMediaManager, err := store.GetRuntimeMediaManager(ctx, mediaManager.Name)
	if err != nil || runtimeMediaManager.Credentials["api_key"] != "encrypted-sonarr-key" || len(runtimeMediaManager.ConfigurationSHA256) != 64 {
		t.Fatalf("GetRuntimeMediaManager() runtime/error = %#v/%v", runtimeMediaManager, err)
	}
	if err := store.RecordMediaManagerHealth(ctx, mediaManager.Name, "ready", map[string]any{"version": "fixture", "response_sha256": strings.Repeat("a", 64)}, actor); err != nil {
		t.Fatal(err)
	}
	if err := store.AuditMediaManagerAction(ctx, mediaManager.Name, "lookup", map[string]any{"query_sha256": strings.Repeat("b", 64)}, actor); err != nil {
		t.Fatal(err)
	}
	mediaManagers, err := store.ListMediaManagers(ctx)
	if err != nil || !slices.ContainsFunc(mediaManagers, func(item MediaManager) bool { return item.ID == mediaManager.ID && item.HealthStatus == "ready" }) {
		t.Fatalf("ListMediaManagers() = %#v/%v", mediaManagers, err)
	}

	metadataProvider, err := store.UpsertMetadataProvider(ctx, "tmdb-"+nameSuffix, MetadataProviderInput{
		Adapter: "tmdb", Enabled: &enabled,
		Config:      EndpointConfig{Endpoint: "https://api.themoviedb.org", Options: map[string]any{"language": "zh-CN"}},
		Credentials: map[string]string{"api_key": "encrypted-tmdb-key"},
	}, actor)
	if err != nil || len(metadataProvider.CredentialFields) != 1 {
		t.Fatalf("UpsertMetadataProvider() provider/error = %#v/%v", metadataProvider, err)
	}
	runtimeMetadataProvider, err := store.GetRuntimeMetadataProvider(ctx, metadataProvider.Name)
	if err != nil || runtimeMetadataProvider.Credentials["api_key"] != "encrypted-tmdb-key" || len(runtimeMetadataProvider.ConfigurationSHA256) != 64 {
		t.Fatalf("GetRuntimeMetadataProvider() runtime/error = %#v/%v", runtimeMetadataProvider, err)
	}
	if err := store.RecordMetadataProviderHealth(ctx, metadataProvider.Name, "ready", map[string]any{"response_count": 1}, actor); err != nil {
		t.Fatal(err)
	}
	afterMetadataHealth, err := store.GetRuntimeMetadataProvider(ctx, metadataProvider.Name)
	if err != nil || afterMetadataHealth.ConfigurationSHA256 != runtimeMetadataProvider.ConfigurationSHA256 {
		t.Fatalf("metadata provider health changed configuration fingerprint: before=%s after=%s error=%v", runtimeMetadataProvider.ConfigurationSHA256, afterMetadataHealth.ConfigurationSHA256, err)
	}
	if err := store.AuditMetadataProviderAction(ctx, metadataProvider.Name, "resolve", map[string]any{"query_sha256": strings.Repeat("c", 64)}, actor); err != nil {
		t.Fatal(err)
	}
	metadataProviders, err := store.ListMetadataProviders(ctx)
	if err != nil || !slices.ContainsFunc(metadataProviders, func(item MetadataProvider) bool {
		return item.ID == metadataProvider.ID && item.HealthStatus == "ready"
	}) {
		t.Fatalf("ListMetadataProviders() = %#v/%v", metadataProviders, err)
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
		UNION ALL SELECT secret_id::text FROM image_hosts WHERE id = $3
		UNION ALL SELECT secret_id::text FROM notification_channels WHERE id = $4
		UNION ALL SELECT secret_id::text FROM media_managers WHERE id = $5
		UNION ALL SELECT secret_id::text FROM metadata_providers WHERE id = $6`, credential.ID, downloader.ID, imageHost.ID, notificationChannel.ID, mediaManager.ID, metadataProvider.ID)
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
	if len(secretIDs) != 7 {
		t.Fatalf("encrypted secret count = %d, want 7", len(secretIDs))
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
