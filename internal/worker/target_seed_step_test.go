package worker

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestTargetSeedVerifyPersistsSatisfiedObservation(t *testing.T) {
	execution, store, evidence, files := targetSeedExecution(t)
	provider := &fakeDownloaderProvider{inspection: evidence, files: files}
	recorder := &sequenceArtifactRecorder{}
	output, err := (targetSeedVerifyExecutor{
		provider: provider, artifacts: store, recorder: recorder, now: func() time.Time { return time.Unix(10_000, 0) },
	}).Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if len(recorder.inputs) != 1 || recorder.inputs[0].Kind != "target_seed_observation" {
		t.Fatalf("seed observation artifacts = %#v", recorder.inputs)
	}
	var result struct {
		Verified bool   `json:"verified"`
		Status   string `json:"status"`
		Hash     string `json:"torrent_hash"`
		Artifact string `json:"observation_artifact_id"`
	}
	if json.Unmarshal(output, &result) != nil || !result.Verified || result.Status != "seeding_requirements_satisfied" ||
		result.Hash != evidence.Torrent.Hash || result.Artifact != "artifact-1" {
		t.Fatalf("target seed output = %s", output)
	}
}

func TestTargetSeedVerifyBlocksUntilRatioAndTimeAreSatisfied(t *testing.T) {
	execution, store, evidence, files := targetSeedExecution(t)
	evidence.Torrent.Ratio = 0.25
	evidence.Torrent.SeedingTime = 120
	files.Torrent = evidence
	recorder := &sequenceArtifactRecorder{}
	_, err := (targetSeedVerifyExecutor{
		provider: &fakeDownloaderProvider{inspection: evidence, files: files}, artifacts: store, recorder: recorder,
	}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_seeding_obligation_pending" || blocked.NextActions[0].Action != "continue_target_seeding" ||
		len(recorder.inputs) != 1 || recorder.inputs[0].Kind != "target_seed_observation" {
		t.Fatalf("pending seed blocker/artifacts = %#v/%#v", blocked, recorder.inputs)
	}
}

func TestTargetSeedVerifyBlocksUnsafeLimitWithEvidence(t *testing.T) {
	execution, store, evidence, files := targetSeedExecution(t)
	evidence.Torrent.UploadLimit = -1
	files.Torrent = evidence
	recorder := &sequenceArtifactRecorder{}
	_, err := (targetSeedVerifyExecutor{
		provider: &fakeDownloaderProvider{inspection: evidence, files: files}, artifacts: store, recorder: recorder,
	}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_seed_policy_mismatch" || blocked.NextActions[0].Action != "repair_target_downloader_policy" || len(recorder.inputs) != 1 {
		t.Fatalf("unsafe limit blocker/artifacts = %#v/%#v", blocked, recorder.inputs)
	}
}

func targetSeedExecution(t *testing.T) (Execution, WorkflowArtifactStore, downloaders.TorrentEvidence, downloaders.TorrentFilesEvidence) {
	t.Helper()
	injectExecution, store, torrent := targetInjectExecution(t, map[string]any{
		"name": "box", "category": "mteam", "tags": []string{"retorrent", "mteam"},
		"upload_limit_bytes_per_second": 4 * 1024 * 1024,
	})
	inspection, err := torrentmeta.Inspect(torrent)
	if err != nil {
		t.Fatal(err)
	}
	addEvidence := targetAddEvidence(torrent, inspection, "box")
	injectOutput, err := (targetInjectExecutor{
		provider: &fakeDownloaderProvider{addResult: addEvidence}, artifacts: store, recorder: &sequenceArtifactRecorder{},
	}).Execute(context.Background(), injectExecution)
	if err != nil {
		t.Fatal(err)
	}
	var frozen struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(injectExecution.Step.InputSnapshot, &frozen); err != nil {
		t.Fatal(err)
	}
	frozen.PreviousSteps["target_inject"] = injectOutput
	torrentEvidence := downloaders.TorrentEvidence{
		DownloaderName: "box", Adapter: "qbittorrent", ConfigurationSHA256: addEvidence.ConfigurationSHA256,
		RemoteSavePath: "/remote/downloads", RemoteContentPath: "/remote/downloads/video.mkv",
		Torrent: qbittorrent.Torrent{
			Hash: inspection.Hashes.V1SHA1, Name: "video.mkv", State: "uploading", Progress: 1,
			Size: 3, TotalSize: 3, Completed: 3, AmountLeft: 0, Ratio: 1.25,
			DownloadLimit: -1, UploadLimit: 2 * 1024 * 1024,
			SavePath: "/remote/downloads", ContentPath: "/remote/downloads/video.mkv",
			Category: "mteam", Tags: "retorrent,mteam", AddedOn: 1, CompletionOn: 2,
			TimeActive: 4_000, SeedingTime: 3_600,
		},
	}
	filesEvidence := downloaders.TorrentFilesEvidence{
		DownloaderName: "box", Adapter: "qbittorrent", Torrent: torrentEvidence,
		Files:     []qbittorrent.TorrentFile{{Index: 0, Name: "video.mkv", Size: 3, Progress: 1, Priority: 1, Seed: true}},
		FileCount: 1, TotalSize: 3,
	}
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "target-seed-step", InputSnapshot: mustJSON(map[string]any{
			"previous_steps": frozen.PreviousSteps,
		})},
		Attempt: workflow.Attempt{ID: "target-seed-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "fixture"},
	}, store, torrentEvidence, filesEvidence
}
