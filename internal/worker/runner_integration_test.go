package worker

import (
	"context"
	"encoding/json"
	"fmt"
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
	"github.com/loofk/upload-assistant/v2/internal/metadataproviders"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/sites/mteam"
	"github.com/loofk/upload-assistant/v2/internal/torrentmaker"
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

	metainfo := targetTorrentMetainfo("https://t.test", "U2", 13, nil)
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	rule := testRuleRevision(t, "source", true)
	targetRule := integrationTargetRuleRevision(t)
	input := mustJSON(map[string]any{
		"source_url": "https://u2.dmhy.org/details.php?id=60635", "target": "MTEAM",
		"accept_rules": map[string]any{"U2": map[string]any{
			"accepted": true, "fingerprint": rule.Fingerprint,
			"obligations": map[string]any{"repost-permission": map[string]any{
				"confirmed": true, "evidence": "Fixture confirms source-side permission was reviewed.",
			}},
		}, "MTEAM": map[string]any{
			"accepted": true, "fingerprint": targetRule.Fingerprint,
			"obligations": map[string]any{"repost-permission": map[string]any{
				"confirmed": true, "evidence": "Fixture confirms target-side upload obligations were reviewed.",
			}},
		}},
		"downloader":         map[string]any{"name": "fixture-box", "save_path": "/remote/downloads"},
		"metadata_providers": map[string]any{"tmdb": "tmdb-fixture", "ptgen": "ptgen-fixture"},
		"confirm_upload":     true,
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
	localContentFile := filepath.Join(localContentRoot, "video.mkv")
	if err := os.WriteFile(localContentFile, []byte("fixture-video"), 0o640); err != nil {
		t.Fatal(err)
	}
	source := fakeSourceProvider{
		info: sites.SourceInfo{Tracker: "U2", TorrentID: "60635", Name: "fixture", IMDbID: "tt1234567", TMDbID: "42", TMDbType: "movie", DoubanID: "1292052", AniDBID: "3456", RetrievedAt: time.Now().UTC()},
		download: sites.DownloadedTorrent{
			Bytes: metainfo, Filename: "U2-60635.torrent", ContentType: "application/x-bittorrent",
			SizeBytes: int64(len(metainfo)), SHA256: sha256String(metainfo), Hashes: hashes,
		},
	}
	downloader := &fakeDownloaderProvider{
		addResults: []downloaders.AddEvidence{{
			DownloaderName: "fixture-box", Adapter: "qbittorrent",
			Result: qbittorrent.AddResult{Hashes: hashes},
		}},
		inspection: downloaders.TorrentEvidence{
			DownloaderName: "fixture-box", Adapter: "qbittorrent",
			Torrent: qbittorrent.Torrent{
				Hash: hashes.V1SHA1, State: "downloading", Progress: 0.5,
				TotalSize: 100, Completed: 50, AmountLeft: 50,
			},
		},
		files: downloadFilesEvidence(hashes.V1SHA1, "/remote/downloads/video.mkv", localContentFile, []qbittorrent.TorrentFile{{
			Index: 0, Name: "video.mkv", Size: 13, Progress: 1, Priority: 1, Availability: 1,
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
	targetTorrentProfiles, err := sites.NewTargetTorrentRegistry(mteam.NewTorrentAdapter())
	if err != nil {
		t.Fatal(err)
	}
	targetTorrentMaker := &fakeTargetTorrentMaker{result: torrentmaker.Result{
		Torrent: targetTorrentMetainfo("https://fake.tracker", "MTEAM", 13, nil),
		Tool:    "mkbrr", Version: "mkbrr version: v1.23.0", Verification: "100.00%",
	}}
	targetUploader := &fakeTargetUploader{result: sites.TargetUploadEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("c", 64),
		TorrentID: "98765", DetailsURL: "https://kp.m-team.cc/details/98765",
		ResponseSHA256: strings.Repeat("e", 64), SubmittedAt: time.Unix(3, 0).UTC(),
	}}
	downloadedTargetTorrent := targetTorrentMetainfo("https://tracker.m-team.cc/announce/fixture-passkey", "MTEAM", 13, nil)
	targetTorrentDownloader := &fakeTargetTorrentDownloader{result: targetTorrentDownloadResult(t, downloadedTargetTorrent)}
	targetInspection, err := torrentmeta.Inspect(downloadedTargetTorrent)
	if err != nil {
		t.Fatal(err)
	}
	downloader.addResults = append(downloader.addResults, targetAddEvidence(downloadedTargetTorrent, targetInspection, "fixture-box"))
	runner := New(
		service, "fixture-worker", slog.New(slog.NewTextHandler(io.Discard, nil)),
		WithRuleProvider(integrationRuleProvider{revisions: map[string]rules.Revision{"U2": rule, "MTEAM": targetRule}}),
		WithSourceAdapters(source, artifactStore),
		WithDownloader(downloader, artifactStore, allowedContentRoot),
		WithMetadata(artifactStore),
		WithMetadataProviders(integrationMetadataResolver{}, artifactStore),
		WithMediaInfo(&fakeMediaInspector{result: media.Inspection{
			Tool: "mediainfo", Version: "fixture", Format: "json", Document: []byte(`{"media":{"track":[{"@type":"Video","Width":"1920","Height":"1080"}]}}`), DurationMS: 1,
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
		WithTargetTorrents(targetTorrentProfiles, targetTorrentMaker, artifactStore),
		WithTargetUploads(targetUploader, targetDuplicates, integrationRuleProvider{
			revisions: map[string]rules.Revision{"U2": rule, "MTEAM": targetRule},
		}, artifactStore),
		WithTargetTorrentDownloads(targetTorrentDownloader, artifactStore),
		WithTargetInjection(downloader, artifactStore),
		WithTargetSeedVerification(downloader, artifactStore),
		WithSummary(artifactStore),
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
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "metadata_tmdb" {
		t.Fatalf("metadata job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 3 || storedArtifacts[2].Kind != "metadata" {
		t.Fatalf("metadata artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("TMDb metadata RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "metadata_ptgen" {
		t.Fatalf("TMDb metadata job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 4 || storedArtifacts[3].Kind != "metadata_tmdb" {
		t.Fatalf("TMDb metadata artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("PTGen metadata RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "media_info" {
		t.Fatalf("PTGen metadata job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 5 || storedArtifacts[4].Kind != "metadata_ptgen" {
		t.Fatalf("PTGen metadata artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("media info RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "screenshots" {
		t.Fatalf("media-info job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 6 || storedArtifacts[5].Kind != "mediainfo" {
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
	if err != nil || len(storedArtifacts) != 7 || storedArtifacts[6].Kind != "screenshot" {
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
	if err != nil || len(storedArtifacts) != 8 || storedArtifacts[7].Kind != "image_upload_receipt" {
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
	if err != nil || len(storedArtifacts) != 9 || storedArtifacts[8].Kind != "target_package" {
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
	if err != nil || len(storedArtifacts) != 10 || storedArtifacts[9].Kind != "duplicate_check" {
		t.Fatalf("target-duplicate artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("target rules RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "target_torrent" {
		t.Fatalf("target-rules job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("target torrent RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "target_upload" {
		t.Fatalf("target-torrent job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 12 || storedArtifacts[10].Kind != "target_torrent" || storedArtifacts[11].Kind != "target_torrent_receipt" {
		t.Fatalf("target-torrent artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("target upload RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "target_torrent_download" {
		t.Fatalf("target-upload job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 14 || storedArtifacts[12].Kind != "preupload_duplicate_check" || storedArtifacts[13].Kind != "target_upload_receipt" {
		t.Fatalf("target-upload artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if targetDuplicates.calls != 2 || targetUploader.calls != 1 || !targetUploader.request.Confirmed {
		t.Fatalf("target upload calls = duplicates:%d uploader:%d confirmed:%t", targetDuplicates.calls, targetUploader.calls, targetUploader.request.Confirmed)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("target torrent download RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "target_inject" {
		t.Fatalf("target-torrent-download job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 16 || storedArtifacts[14].Kind != "target_downloaded_torrent" || storedArtifacts[15].Kind != "target_torrent_download_receipt" {
		t.Fatalf("target-torrent-download artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if targetTorrentDownloader.calls != 1 || targetTorrentDownloader.request.TorrentID != "98765" || targetTorrentDownloader.request.UploadReceiptSHA256 == "" {
		t.Fatalf("target torrent download calls/request = %d/%#v", targetTorrentDownloader.calls, targetTorrentDownloader.request)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("target injection RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "target_seed_verify" {
		t.Fatalf("target-injection job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 17 || storedArtifacts[16].Kind != "target_injection_receipt" {
		t.Fatalf("target-injection artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if downloader.addCalls != 2 || downloader.addOptions.SavePath != "/remote/downloads" || downloader.addOptions.SkipChecking || downloader.addOptions.Paused {
		t.Fatalf("target injection calls/options = %d/%#v", downloader.addCalls, downloader.addOptions)
	}
	targetSeedEvidence := downloaders.TorrentEvidence{
		DownloaderName: "fixture-box", Adapter: "qbittorrent", ConfigurationSHA256: strings.Repeat("d", 64),
		RemoteSavePath: "/remote/downloads", RemoteContentPath: "/remote/downloads/video.mkv",
		Torrent: qbittorrent.Torrent{
			Hash: targetInspection.Hashes.V1SHA1, Name: "video.mkv", State: "uploading", Progress: 1,
			Size: 13, TotalSize: 13, Completed: 13, AmountLeft: 0, DownloadLimit: -1, UploadLimit: -1,
			SavePath: "/remote/downloads", ContentPath: "/remote/downloads/video.mkv", Category: "mteam", Tags: "retorrent,mteam",
		},
	}
	downloader.inspection = targetSeedEvidence
	downloader.files = downloaders.TorrentFilesEvidence{
		DownloaderName: "fixture-box", Adapter: "qbittorrent", Torrent: targetSeedEvidence,
		Files:     []qbittorrent.TorrentFile{{Index: 0, Name: "video.mkv", Size: 13, Progress: 1, Priority: 1, Seed: true}},
		FileCount: 1, TotalSize: 13,
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("target seed verification RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobQueued || job.CurrentStep != "summary" {
		t.Fatalf("target-seed job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 18 || storedArtifacts[17].Kind != "target_seed_observation" {
		t.Fatalf("target-seed artifacts/error = %#v/%v", storedArtifacts, err)
	}
	if err := runner.RunOnce(ctx); err != nil {
		t.Fatalf("summary RunOnce() error = %v", err)
	}
	job, err = service.GetJob(ctx, job.ID)
	if err != nil || job.Status != workflow.JobComplete || job.CurrentStep != "" || job.FinishedAt == nil {
		t.Fatalf("completed job status/current/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	storedArtifacts, err = service.ListArtifacts(ctx, job.ID)
	if err != nil || len(storedArtifacts) != 19 || storedArtifacts[18].Kind != "job_summary" {
		t.Fatalf("summary artifacts/error = %#v/%v", storedArtifacts, err)
	}
	var finalSummary struct {
		OK          bool            `json:"ok"`
		Status      string          `json:"status"`
		JobID       string          `json:"job_id"`
		SummaryFile summaryArtifact `json:"summary_file"`
	}
	if err := json.Unmarshal(job.Summary, &finalSummary); err != nil || !finalSummary.OK || finalSummary.Status != "complete" ||
		finalSummary.JobID != job.ID || finalSummary.SummaryFile.ArtifactID != storedArtifacts[18].ID || finalSummary.SummaryFile.SHA256 != storedArtifacts[18].SHA256 {
		t.Fatalf("final job summary/error = %s/%v", job.Summary, err)
	}
	events, err := service.ListEvents(ctx, job.ID, 0, 200)
	if err != nil || workflow.VerifyEventChain(events) != nil {
		t.Fatalf("event chain/error = %d/%v", len(events), err)
	}
}

type integrationRuleProvider struct {
	revisions map[string]rules.Revision
}

type integrationMetadataResolver struct{}

func (integrationMetadataResolver) Resolve(_ context.Context, name string, request metadataproviders.ResolveRequest, _ workflow.Actor) (metadataproviders.ResolveResult, error) {
	identity := metadataproviders.Identity{
		IMDbID: request.IMDbID, TMDbID: request.TMDbID, TMDbType: request.TMDbType, DoubanID: request.DoubanID,
	}
	result := metadataproviders.ResolveResult{
		Name: name, Matched: true, Identity: identity, ConfigurationSHA256: strings.Repeat("7", 64),
		QuerySHA256: strings.Repeat("8", 64), Calls: []metadataproviders.CallEvidence{{
			Sequence: 1, Purpose: "fixture", QuerySHA256: strings.Repeat("a", 64), ResponseSHA256: strings.Repeat("b", 64), StatusCode: 200,
		}},
	}
	switch name {
	case "tmdb-fixture":
		result.Adapter = "tmdb"
	case "ptgen-fixture":
		result.Adapter = "ptgen"
		result.Description = "Fixture PTGen/Douban description"
		result.DescriptionSHA256 = sha256Hex([]byte(result.Description))
	default:
		return metadataproviders.ResolveResult{}, fmt.Errorf("unexpected fixture metadata provider %s", name)
	}
	return result, nil
}

func (provider integrationRuleProvider) Active(_ context.Context, siteCode string) (rules.Revision, error) {
	revision, exists := provider.revisions[siteCode]
	if !exists {
		return rules.Revision{}, rules.ErrNotFound
	}
	return revision, nil
}

func integrationTargetRuleRevision(t *testing.T) rules.Revision {
	t.Helper()
	revision := testRuleRevision(t, "target", true)
	var policy rules.Policy
	if err := json.Unmarshal(revision.Policy, &policy); err != nil {
		t.Fatal(err)
	}
	policy.Site.Code, policy.Site.DisplayName = "MTEAM", "M-Team"
	policy.Source.URL = "https://wiki.m-team.cc/zh-tw/site-rules"
	body, err := json.Marshal(policy)
	if err != nil {
		t.Fatal(err)
	}
	revision.ID = "6ebaf982-1d6e-4eb3-a22e-c79038ca1851"
	revision.SiteCode = "MTEAM"
	revision.Fingerprint = strings.Repeat("9", 64)
	revision.Policy = body
	return revision
}

func mustTargetPackageRegistry(t *testing.T) *sites.TargetPackageRegistry {
	t.Helper()
	registry, err := sites.NewTargetPackageRegistry(mteam.NewPackageAdapter())
	if err != nil {
		t.Fatal(err)
	}
	return registry
}
