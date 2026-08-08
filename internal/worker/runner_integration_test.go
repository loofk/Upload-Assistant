package worker

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/imagehosts"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/media"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/sites/mteam"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestRunnerPersistsSourceAndDownloaderBoundaryEvidence(t *testing.T) {
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
	store := workflow.NewStore(pool)
	definition := workflow.RetorrentDefinition()
	workflowID, err := store.EnsureDefinition(ctx, definition)
	if err != nil {
		t.Fatal(err)
	}
	service := workflow.NewService(store, definition, workflowID)

	metainfo := []byte("d8:announce14:https://t.test4:infod4:name7:fixtureee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	rule := testRuleRevision(t, "source", true)
	input := mustJSON(map[string]any{
		"source_url": "https://u2.dmhy.org/details.php?id=60635", "target": "MTEAM",
		"accept_rules": map[string]any{"U2": map[string]any{
			"accepted": true, "fingerprint": rule.Fingerprint,
			"obligations": map[string]any{"repost-permission": map[string]any{
				"confirmed": true, "evidence": "Fixture confirms source-side permission was reviewed.",
			}},
		}},
		"downloader": map[string]any{"name": "fixture-box", "save_path": "/remote/downloads"},
	})
	job, err := service.CreateJob(ctx, workflow.CreateJobInput{
		Kind: "retorrent", ExecutionMode: workflow.ExecutionAuto, Input: input,
		IdempotencyKey: "worker-fixture-" + uuid.NewString(), Owner: "integration-test",
		Actor: workflow.Actor{Type: "test", ID: "worker-integration"},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM jobs WHERE id = $1", job.ID) })

	artifactStore, err := artifacts.NewLocalStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	allowedContentRoot := filepath.Join(t.TempDir(), "downloads")
	localContentRoot := filepath.Join(allowedContentRoot, "release")
	if err := os.MkdirAll(localContentRoot, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(localContentRoot, "video.mkv"), []byte("fixture-video"), 0o640); err != nil {
		t.Fatal(err)
	}
	source := fakeSourceProvider{
		info: sites.SourceInfo{Tracker: "U2", TorrentID: "60635", Name: "fixture", IMDbID: "tt1234567", AniDBID: "3456", RetrievedAt: time.Now().UTC()},
		download: sites.DownloadedTorrent{
			Bytes: metainfo, Filename: "U2-60635.torrent", ContentType: "application/x-bittorrent",
			SizeBytes: int64(len(metainfo)), SHA256: sha256String(metainfo), Hashes: hashes,
		},
	}
	downloader := &fakeDownloaderProvider{
		addResult: downloaders.AddEvidence{
			DownloaderName: "fixture-box", Adapter: "qbittorrent",
			Result: qbittorrent.AddResult{Hashes: hashes},
		},
		inspection: downloaders.TorrentEvidence{
			DownloaderName: "fixture-box", Adapter: "qbittorrent",
			Torrent: qbittorrent.Torrent{
				Hash: hashes.V1SHA1, State: "downloading", Progress: 0.5,
				TotalSize: 100, Completed: 50, AmountLeft: 50,
			},
		},
		files: downloadFilesEvidence(hashes.V1SHA1, "/remote/downloads/release", localContentRoot, []qbittorrent.TorrentFile{{
			Index: 0, Name: "release/video.mkv", Size: 13, Progress: 1, Priority: 1, Availability: 1,
		}}),
	}
	imageHost := &fakeImageHostProvider{
		snapshot: imagehosts.HostSnapshot{
			ID: "host-id", Name: "default", Adapter: "imgbb",
			ConfigSHA256: "fixture-config-sha", ConfigurationTime: time.Unix(1, 0).UTC(),
		},
		result: imagehosts.UploadEvidence{
			ImageHostID: "host-id", ImageHostName: "default", Adapter: "imgbb",
			ConfigSHA256: "fixture-config-sha", ConfigurationTime: time.Unix(1, 0).UTC(),
			Result: imagehosts.UploadResult{URL: "https://i.ibb.co/fixture/image.png", ViewerURL: "https://ibb.co/fixture"},
		},
	}
	targetDuplicates := &fakeTargetDuplicateChecker{result: mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("a", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt1234567"}, Candidates: []mteam.DuplicateCandidate{}, CheckedAt: time.Unix(1, 0).UTC(),
	}}
	runner := New(
		service, "fixture-worker", slog.New(slog.NewTextHandler(io.Discard, nil)),
		WithRuleProvider(fakeRuleProvider{revision: rule}),
		WithSourceAdapters(source, artifactStore),
		WithDownloader(downloader, artifactStore, allowedContentRoot),
		WithMetadata(artifactStore),
		WithMediaInfo(&fakeMediaInspector{result: media.Inspection{
			Tool: "mediainfo", Version: "fixture", Document: json.RawMessage(`{"media":{"track":[{"@type":"Video","Width":"1920","Height":"1080"}]}}`), DurationMS: 1,
		}}, artifactStore),
		WithScreenshots(
			fakeScreenshotProfiles{profile: integrations.RuntimeScreenshotProfile{
				ScreenshotProfile: integrations.ScreenshotProfile{
					ID: "profile-id", Name: "default", Revision: 1, Enabled: true,
					Config: json.RawMessage(`{"count":1,"format":"png","quality":90,"start_percent":0.1,"end_percent":0.9}`),
				},
				ScreenshotConfig: integrations.ScreenshotConfig{Count: 1, Format: "png", Quality: 90, StartPercent: 0.1, EndPercent: 0.9},
			}},
			&fakeScreenshotGenerator{batch: media.ScreenshotBatch{
				Tool: "ffmpeg", Version: "fixture", DurationSeconds: 100,
				Screenshots: []media.Screenshot{{
					Index: 1, Timestamp: 50, Format: "png", Filename: "screenshot-01.png",
					MIMEType: "image/png", Bytes: []byte("\x89PNG\r\n\x1a\nfixture"), SizeBytes: 15,
				}},
			}},
			artifactStore,
		),
		WithImageHosts(imageHost, artifactStore),
		WithTargetPackages(mustTargetPackageRegistry(t), artifactStore),
		WithTargetDuplicateChecks(targetDuplicates, artifactStore),
	)
	for iteration := 0; iteration < 6; iteration++ {
		if err := runner.RunOnce(ctx); err != nil {
			t.Fatalf("RunOnce(%d) error = %v", iteration+1, err)
		}
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != workflow.JobBlocked || job.CurrentStep != "downloader_wait" {
		t.Fatalf("job status/current = %s/%s", job.Status, job.CurrentStep)
	}
	var blockers []Blocker
	if err := json.Unmarshal(job.Blockers, &blockers); err != nil || len(blockers) != 1 || blockers[0].Code != "source_download_incomplete" {
		t.Fatalf("job blockers/error = %#v/%v", blockers, err)
	}
	storedArtifacts, err := service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 1 || storedArtifacts[0].SHA256 != sha256String(metainfo) {
		t.Fatalf("artifacts/error = %#v/%v", storedArtifacts, err)
	}
	steps, err := service.ListSteps(ctx, job.ID)
	if err != nil {
		t.Fatal(err)
	}
	for index, key := range []string{"source_parse", "source_inspect", "source_rules", "source_torrent", "downloader_add"} {
		if steps[index].Key != key || steps[index].Status != workflow.StepComplete {
			t.Fatalf("step %d = %s/%s", index, steps[index].Key, steps[index].Status)
		}
	}
	downloader.inspection.Torrent.State = "uploading"
	downloader.inspection.Torrent.Progress = 1
	downloader.inspection.Torrent.Completed = 100
	downloader.inspection.Torrent.AmountLeft = 0
	job, err = service.ResumeJob(ctx, job.ID, json.RawMessage(`{}`), workflow.Actor{Type: "test", ID: "worker-integration"})
	if err != nil || job.Status != workflow.JobQueued {
		t.Fatalf("ResumeJob() job/error = %s/%v", job.Status, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("resumed RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "content_resolve" {
		t.Fatalf("resumed job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	steps, err = service.ListSteps(ctx, job.ID)
	if err != nil || steps[5].Status != workflow.StepComplete {
		t.Fatalf("resumed downloader_wait status/error = %s/%v", steps[5].Status, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("content resolve RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "metadata" {
		t.Fatalf("content-resolved job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 2 || storedArtifacts[1].Kind != "content_manifest" {
		t.Fatalf("content-resolved artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("metadata RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "media_info" {
		t.Fatalf("metadata job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 3 || storedArtifacts[2].Kind != "metadata" {
		t.Fatalf("metadata artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("media info RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "screenshots" {
		t.Fatalf("media-info job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 4 || storedArtifacts[3].Kind != "mediainfo" {
		t.Fatalf("media-info artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("screenshots RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "image_upload" {
		t.Fatalf("screenshots job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 5 || storedArtifacts[4].Kind != "screenshot" {
		t.Fatalf("screenshot artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("image upload RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "target_package" {
		t.Fatalf("image-upload job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 6 || storedArtifacts[5].Kind != "image_upload_receipt" {
		t.Fatalf("image-upload artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("target package RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "target_duplicate_check" {
		t.Fatalf("target-package job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 7 || storedArtifacts[6].Kind != "target_package" {
		t.Fatalf("target-package artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("target duplicate-check RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "target_rules" {
		t.Fatalf("target-duplicate job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 8 || storedArtifacts[7].Kind != "duplicate_check" {
		t.Fatalf("target-duplicate artifacts/error = %#v/%v", storedArtifacts, err)
	}
	events, err := service.ListEvents(ctx, job.ID, 0, 200)
	if err != nil || workflow.VerifyEventChain(events) != nil {
		t.Fatalf("event chain/error = %d/%v", len(events), err)
	}
}

func mustTargetPackageRegistry(t *testing.T) *sites.TargetPackageRegistry {
	t.Helper()
	registry, err := sites.NewTargetPackageRegistry(mteam.NewPackageAdapter())
	if err != nil {
		t.Fatal(err)
	}
	return registry
}
