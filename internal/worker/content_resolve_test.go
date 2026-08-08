package worker

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestContentResolveVerifiesFilesAndWritesManifest(t *testing.T) {
	allowedRoot := filepath.Join(t.TempDir(), "downloads")
	contentRoot := filepath.Join(allowedRoot, "release")
	if err := os.MkdirAll(contentRoot, 0o750); err != nil {
		t.Fatal(err)
	}
	videoPath := filepath.Join(contentRoot, "video.mkv")
	if err := os.WriteFile(videoPath, []byte("fixture-video"), 0o640); err != nil {
		t.Fatal(err)
	}
	hash := "0123456789abcdef0123456789abcdef01234567"
	provider := &fakeDownloaderProvider{files: downloadFilesEvidence(
		hash, "/remote/downloads/release", contentRoot,
		[]qbittorrent.TorrentFile{{Index: 0, Name: "release/video.mkv", Size: 13, Progress: 1, Priority: 1, Availability: 1}},
	)}
	artifactStore, err := artifacts.NewLocalStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	recorder := &fakeArtifactRecorder{}
	executor := contentResolveExecutor{
		provider: provider, artifacts: artifactStore, recorder: recorder,
		allowedRoots: []string{allowedRoot},
	}
	output, err := executor.Execute(context.Background(), contentExecution(hash))
	if err != nil {
		t.Fatal(err)
	}
	if recorder.recorded.Kind != "content_manifest" || recorder.recorded.SHA256 == "" {
		t.Fatalf("recorded manifest = %#v", recorder.recorded)
	}
	var result struct {
		Resolved        bool     `json:"resolved"`
		FileCount       int      `json:"file_count"`
		MediaCandidates []string `json:"media_candidates"`
	}
	if err := json.Unmarshal(output, &result); err != nil || !result.Resolved || result.FileCount != 1 || len(result.MediaCandidates) != 1 || result.MediaCandidates[0] != videoPath {
		t.Fatalf("content output/error = %#v/%v", result, err)
	}
	manifestFile, err := artifactStore.Open(recorder.recorded.StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	defer manifestFile.Close()
	var manifest contentManifest
	if err := json.NewDecoder(manifestFile).Decode(&manifest); err != nil || manifest.FileCount != 1 || manifest.ResolvedFiles[0].LocalPath != videoPath {
		t.Fatalf("manifest/error = %#v/%v", manifest, err)
	}
}

func TestContentResolveBlocksMissingMappingAndUnsafeTorrentPath(t *testing.T) {
	hash := "0123456789abcdef0123456789abcdef01234567"
	provider := &fakeDownloaderProvider{files: downloadFilesEvidence(
		hash, "/remote/release", "",
		[]qbittorrent.TorrentFile{{Index: 0, Name: "video.mkv", Size: 1, Progress: 1}},
	)}
	executor := contentResolveExecutor{provider: provider, artifacts: mustArtifactStore(t), recorder: &fakeArtifactRecorder{}, allowedRoots: []string{t.TempDir()}}
	_, err := executor.Execute(context.Background(), contentExecution(hash))
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "downloader_path_mapping_required" {
		t.Fatalf("missing mapping blocker = %#v", blocked)
	}

	allowedRoot := t.TempDir()
	contentRoot := filepath.Join(allowedRoot, "release")
	if err := os.MkdirAll(contentRoot, 0o750); err != nil {
		t.Fatal(err)
	}
	provider.files = downloadFilesEvidence(hash, "/remote/release", contentRoot, []qbittorrent.TorrentFile{{
		Index: 0, Name: "../escape.mkv", Size: 1, Progress: 1,
	}})
	executor.allowedRoots = []string{allowedRoot}
	_, err = executor.Execute(context.Background(), contentExecution(hash))
	blocked = requireBlockError(t, err)
	if blocked.Blockers[0].Code != "content_verification_failed" {
		t.Fatalf("unsafe path blocker = %#v", blocked)
	}
}

func downloadFilesEvidence(hash, remoteRoot, localRoot string, files []qbittorrent.TorrentFile) downloaders.TorrentFilesEvidence {
	var total int64
	for _, file := range files {
		total += file.Size
	}
	return downloaders.TorrentFilesEvidence{
		DownloaderName: "box", Adapter: "qbittorrent", Files: files, FileCount: len(files), TotalSize: total,
		Torrent: downloaders.TorrentEvidence{
			DownloaderName: "box", Adapter: "qbittorrent", RemoteContentPath: remoteRoot, LocalContentPath: localRoot,
			Torrent: qbittorrent.Torrent{Hash: hash, ContentPath: remoteRoot, TotalSize: total, Progress: 1},
		},
	}
}

func contentExecution(hash string) Execution {
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "content-step", InputSnapshot: mustJSON(map[string]any{
			"previous_steps": map[string]any{"downloader_wait": map[string]any{
				"completed": true, "downloader_name": "box", "torrent_hash": hash,
			}},
		})},
		Attempt: workflow.Attempt{ID: "content-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "test-worker"},
	}
}

func mustArtifactStore(t *testing.T) *artifacts.LocalStore {
	t.Helper()
	store, err := artifacts.NewLocalStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return store
}
