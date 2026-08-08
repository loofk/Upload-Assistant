package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeDownloaderProvider struct {
	addOptions qbittorrent.AddOptions
	addBytes   []byte
	addResult  downloaders.AddEvidence
	inspection downloaders.TorrentEvidence
	err        error
}

func (provider *fakeDownloaderProvider) Add(_ context.Context, _ string, metainfo []byte, options qbittorrent.AddOptions, _ workflow.Actor) (downloaders.AddEvidence, error) {
	provider.addBytes = append([]byte(nil), metainfo...)
	provider.addOptions = options
	return provider.addResult, provider.err
}

func (provider *fakeDownloaderProvider) Inspect(_ context.Context, _, _ string, _ workflow.Actor) (downloaders.TorrentEvidence, error) {
	return provider.inspection, provider.err
}

func TestDownloaderAddVerifiesArtifactAndAppliesStrictestRuleLimits(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod4:name7:fixtureee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	store, err := artifacts.NewLocalStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	written, err := store.Write(context.Background(), artifacts.Scope{
		JobID: "job-id", StepID: "source-step", AttemptID: "source-attempt",
	}, "source.torrent", bytes.NewReader(metainfo))
	if err != nil {
		t.Fatal(err)
	}
	provider := &fakeDownloaderProvider{addResult: downloaders.AddEvidence{
		DownloaderName: "box", Adapter: "qbittorrent",
		Result: qbittorrent.AddResult{Hashes: hashes},
	}}
	execution := downloaderAddExecution(written.RelativePath, written.SizeBytes, written.SHA256)
	executor := downloaderAddExecutor{provider: provider, artifacts: store}
	output, err := executor.Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if string(provider.addBytes) != string(metainfo) || provider.addOptions.DownloadLimit != 20*1024*1024 ||
		provider.addOptions.UploadLimit != 50_000_000 || provider.addOptions.SavePath != "/remote/downloads" {
		t.Fatalf("applied add bytes/options = %q/%#v", provider.addBytes, provider.addOptions)
	}
	var result struct {
		DownloaderName string `json:"downloader_name"`
		TorrentHash    string `json:"torrent_hash"`
	}
	if err := json.Unmarshal(output, &result); err != nil || result.DownloaderName != "box" || result.TorrentHash != hashes.V1SHA1 {
		t.Fatalf("downloader add output/error = %#v/%v", result, err)
	}
}

func TestDownloaderWaitBlocksWithObservedProgressAndCompletesOnResume(t *testing.T) {
	provider := &fakeDownloaderProvider{inspection: downloaders.TorrentEvidence{
		DownloaderName: "box", Adapter: "qbittorrent",
		Torrent: qbittorrent.Torrent{Hash: "0123456789abcdef0123456789abcdef01234567", State: "downloading", Progress: 0.42, TotalSize: 100, Completed: 42, AmountLeft: 58},
	}}
	executor := downloaderWaitExecutor{provider: provider}
	execution := Execution{
		Step: workflow.Step{InputSnapshot: mustJSON(map[string]any{"previous_steps": map[string]any{
			"downloader_add": map[string]any{"downloader_name": "box", "torrent_hash": provider.inspection.Torrent.Hash},
		}})},
		Actor: workflow.Actor{Type: "worker", ID: "test-worker"},
	}
	_, err := executor.Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "source_download_incomplete" || blocked.NextActions[0].Action != "resume_job_when_download_progresses" {
		t.Fatalf("blocked = %#v", blocked)
	}

	provider.inspection.Torrent.Progress = 1
	provider.inspection.Torrent.Completed = 100
	provider.inspection.Torrent.AmountLeft = 0
	output, err := executor.Execute(context.Background(), execution)
	var completed struct {
		Completed bool `json:"completed"`
	}
	decodeErr := json.Unmarshal(output, &completed)
	if err != nil || decodeErr != nil || !completed.Completed {
		t.Fatalf("completed output/error = %s/%v", output, err)
	}
}

func downloaderAddExecution(storagePath string, size int64, sha string) Execution {
	return Execution{
		Job: workflow.Job{ID: "job-id"},
		Step: workflow.Step{ID: "downloader-step", InputSnapshot: mustJSON(map[string]any{
			"job_input": map[string]any{"downloader": map[string]any{
				"name": "box", "save_path": "/remote/downloads", "category": "retorrent",
				"tags": []string{"source", "U2"}, "download_limit_bytes_per_second": 40_000_000,
				"upload_limit_bytes_per_second": 50_000_000,
			}},
			"previous_steps": map[string]any{
				"source_torrent": map[string]any{"storage_path": storagePath, "size_bytes": size, "sha256": sha},
				"source_rules": map[string]any{
					"fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
					"limits":      map[string]any{"download": "20MiB/s", "upload": "100M/s"},
				},
			},
		})},
		Attempt: workflow.Attempt{ID: "downloader-attempt"},
		Actor:   workflow.Actor{Type: "worker", ID: "test-worker"},
	}
}
