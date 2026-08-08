package worker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeSourceProvider struct {
	info     sites.SourceInfo
	download sites.DownloadedTorrent
	err      error
}

func (provider fakeSourceProvider) Inspect(context.Context, sites.SourceReference) (sites.SourceInfo, error) {
	return provider.info, provider.err
}
func (provider fakeSourceProvider) Download(context.Context, sites.SourceReference) (sites.DownloadedTorrent, error) {
	return provider.download, provider.err
}

type fakeArtifactRecorder struct {
	recorded workflow.RegisterArtifactInput
}

func (recorder *fakeArtifactRecorder) RegisterArtifact(_ context.Context, input workflow.RegisterArtifactInput) (workflow.Artifact, error) {
	recorder.recorded = input
	return workflow.Artifact{
		ID: "artifact-id", JobID: input.JobID, StepID: input.StepID, AttemptID: input.AttemptID,
		Kind: input.Kind, StorageBackend: "local", StoragePath: input.StoragePath,
		Filename: input.Filename, MIMEType: input.MIMEType, SizeBytes: input.SizeBytes,
		SHA256: input.SHA256, Metadata: input.Metadata,
	}, nil
}

func TestSourceInspectUsesFrozenReference(t *testing.T) {
	execution := sourceExecution(t)
	executor := sourceInspectExecutor{provider: fakeSourceProvider{info: sites.SourceInfo{
		Tracker: "U2", TorrentID: "60635", Name: "fixture",
	}}}
	output, err := executor.Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	var result struct {
		SourceInfo sites.SourceInfo `json:"source_info"`
	}
	if err := json.Unmarshal(output, &result); err != nil || result.SourceInfo.Name != "fixture" {
		t.Fatalf("source inspection output/error = %#v/%v", result, err)
	}
}

func TestSourceInspectStoresDescriptionAsSeparateArtifact(t *testing.T) {
	store := mustArtifactStore(t)
	recorder := &fakeArtifactRecorder{}
	execution := sourceExecution(t)
	executor := sourceInspectExecutor{
		provider: fakeSourceProvider{info: sites.SourceInfo{
			Tracker: "U2", TorrentID: "60635", Name: "fixture",
			DescriptionHTML: "<p>source description</p>", DescriptionLength: 18,
		}},
		artifacts: store, recorder: recorder,
	}
	output, err := executor.Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if recorder.recorded.Kind != "source_description" || recorder.recorded.SHA256 == "" {
		t.Fatalf("description artifact = %#v", recorder.recorded)
	}
	if string(output) == "" || json.Valid(output) == false {
		t.Fatalf("description output = %s", output)
	}
}

func TestSourceTorrentPersistsAuditedArtifact(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod4:name7:fixtureee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	store, err := artifacts.NewLocalStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	recorder := &fakeArtifactRecorder{}
	executor := sourceTorrentExecutor{
		provider: fakeSourceProvider{download: sites.DownloadedTorrent{
			Bytes: metainfo, Filename: "U2-60635.torrent", ContentType: "application/x-bittorrent",
			SizeBytes: int64(len(metainfo)), SHA256: sha256String(metainfo), Hashes: hashes,
		}}, artifacts: store, recorder: recorder,
	}
	output, err := executor.Execute(context.Background(), sourceExecution(t))
	if err != nil {
		t.Fatal(err)
	}
	if recorder.recorded.Kind != "source_torrent" || recorder.recorded.Retention != artifactRetention || recorder.recorded.SHA256 == "" {
		t.Fatalf("recorded artifact = %#v", recorder.recorded)
	}
	var result struct {
		ArtifactID string `json:"artifact_id"`
		SHA256     string `json:"sha256"`
	}
	if err := json.Unmarshal(output, &result); err != nil || result.ArtifactID != "artifact-id" || result.SHA256 == "" {
		t.Fatalf("source torrent output/error = %#v/%v", result, err)
	}
}

func TestSourceAdapterFailureBecomesRecoverableBlock(t *testing.T) {
	executor := sourceInspectExecutor{provider: fakeSourceProvider{err: sites.NewAdapterError(
		"site_cookie_required", "cookie required", false, nil,
	)}}
	_, err := executor.Execute(context.Background(), sourceExecution(t))
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "site_cookie_required" || blocked.NextActions[0].Action != "configure_site_credentials" {
		t.Fatalf("blocked = %#v", blocked)
	}
}

func sourceExecution(t *testing.T) Execution {
	t.Helper()
	return Execution{
		Job: workflow.Job{ID: "job-id"},
		Step: workflow.Step{ID: "step-id", InputSnapshot: mustJSON(map[string]any{
			"previous_steps": map[string]any{
				"source_parse": map[string]any{"source": sites.SourceReference{Tracker: "U2", TorrentID: "60635"}},
			},
		})},
		Attempt: workflow.Attempt{ID: "attempt-id"},
		Actor:   workflow.Actor{Type: "worker", ID: "test-worker"},
	}
}

func sha256String(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}
