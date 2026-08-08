package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestTargetInjectStepAppliesStrictRuleLimitAndPersistsReceipt(t *testing.T) {
	execution, store, torrent := targetInjectExecution(t, map[string]any{
		"name": "box", "category": "MTEAM", "tags": []string{"retorrent", "mteam"},
		"upload_limit_bytes_per_second": 4 * 1024 * 1024,
	})
	inspection, err := torrentmeta.Inspect(torrent)
	if err != nil {
		t.Fatal(err)
	}
	provider := &fakeDownloaderProvider{addResult: targetAddEvidence(torrent, inspection, "box")}
	recorder := &sequenceArtifactRecorder{}
	output, err := (targetInjectExecutor{provider: provider, artifacts: store, recorder: recorder}).Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if provider.addCalls != 1 || provider.addName != "box" || !bytes.Equal(provider.addBytes, torrent) ||
		provider.addOptions.SavePath != "/remote/downloads" || provider.addOptions.UploadLimit != 2*1024*1024 ||
		provider.addOptions.DownloadLimit != 0 || provider.addOptions.SkipChecking || provider.addOptions.Paused ||
		len(recorder.inputs) != 1 || recorder.inputs[0].Kind != "target_injection_receipt" {
		t.Fatalf("target add/options/artifacts = %#v/%#v", provider, recorder.inputs)
	}
	var result struct {
		Injected   bool   `json:"injected"`
		Status     string `json:"status"`
		Hash       string `json:"torrent_hash"`
		ReceiptID  string `json:"receipt_artifact_id"`
		RemotePath string `json:"expected_remote_content_path"`
	}
	if json.Unmarshal(output, &result) != nil || !result.Injected || result.Status != "injected" ||
		result.Hash != inspection.Hashes.V1SHA1 || result.ReceiptID != "artifact-1" || result.RemotePath != "/remote/downloads/video.mkv" {
		t.Fatalf("target injection output = %s", output)
	}
	file, err := store.Open(recorder.inputs[0].StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	receipt, _ := io.ReadAll(file)
	file.Close()
	if !bytes.Contains(receipt, []byte(`"configuration_sha256"`)) || !bytes.Contains(receipt, []byte(`"upload_limit_bytes_per_second": 2097152`)) {
		t.Fatalf("target injection receipt = %s", receipt)
	}
}

func TestTargetInjectStepRejectsSkipCheckingBeforeDownloaderCall(t *testing.T) {
	execution, store, _ := targetInjectExecution(t, map[string]any{"name": "box", "skip_checking": true})
	provider := &fakeDownloaderProvider{}
	_, err := (targetInjectExecutor{provider: provider, artifacts: store, recorder: &sequenceArtifactRecorder{}}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_downloader_configuration_invalid" || provider.addCalls != 0 {
		t.Fatalf("skip-check blocker/provider = %#v/%#v", blocked, provider)
	}
}

func TestTargetInjectStepRejectsUnboundDownloaderEvidence(t *testing.T) {
	execution, store, torrent := targetInjectExecution(t, map[string]any{"name": "box"})
	inspection, _ := torrentmeta.Inspect(torrent)
	evidence := targetAddEvidence(torrent, inspection, "box")
	evidence.ConfigurationSHA256 = ""
	provider := &fakeDownloaderProvider{addResult: evidence}
	recorder := &sequenceArtifactRecorder{}
	_, err := (targetInjectExecutor{provider: provider, artifacts: store, recorder: recorder}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_injection_evidence_invalid" || blocked.NextActions[0].Action != "inspect_target_torrent_in_downloader" || len(recorder.inputs) != 0 {
		t.Fatalf("unbound evidence blocker/artifacts = %#v/%#v", blocked, recorder.inputs)
	}
}

func TestTargetInjectStepAcceptsCapabilityLimitedAdapterWithExplicitNoLabelMode(t *testing.T) {
	execution, store, torrent := targetInjectExecution(t, map[string]any{"name": "box", "apply_labels": false})
	inspection, err := torrentmeta.Inspect(torrent)
	if err != nil {
		t.Fatal(err)
	}
	evidence := targetAddEvidence(torrent, inspection, "box")
	evidence.Adapter = "deluge"
	evidence.Observed.Adapter = "deluge"
	provider := &fakeDownloaderProvider{addResult: evidence}
	output, err := (targetInjectExecutor{provider: provider, artifacts: store, recorder: &sequenceArtifactRecorder{}}).Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if provider.addOptions.Category != "" || len(provider.addOptions.Tags) != 0 {
		t.Fatalf("Deluge target add options = %#v", provider.addOptions)
	}
	var result struct {
		Options targetInjectOptionsReceipt `json:"options"`
	}
	if json.Unmarshal(output, &result) != nil || result.Options.ApplyLabels {
		t.Fatalf("Deluge target injection output = %s", output)
	}
}

func targetInjectExecution(t *testing.T, targetControl map[string]any) (Execution, WorkflowArtifactStore, []byte) {
	t.Helper()
	downloadExecution, store, _ := targetTorrentDownloadExecution(t)
	torrent := targetTorrentMetainfo("https://tracker.m-team.cc/announce/secret-passkey", "MTEAM", 3, nil)
	downloadOutput, err := (targetTorrentDownloadExecutor{
		downloader: &fakeTargetTorrentDownloader{result: targetTorrentDownloadResult(t, torrent)},
		artifacts:  store, recorder: &sequenceArtifactRecorder{},
	}).Execute(context.Background(), downloadExecution)
	if err != nil {
		t.Fatal(err)
	}
	var frozen struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(downloadExecution.Step.InputSnapshot, &frozen); err != nil {
		t.Fatal(err)
	}
	frozen.PreviousSteps["target_torrent_download"] = downloadOutput
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "target-inject-step", InputSnapshot: mustJSON(map[string]any{
			"job_input": map[string]any{"target_downloader": targetControl}, "resume_state": map[string]any{},
			"previous_steps": frozen.PreviousSteps,
		})},
		Attempt: workflow.Attempt{ID: "target-inject-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "fixture"},
	}, store, torrent
}

func targetAddEvidence(torrent []byte, inspection torrentmeta.Inspection, downloaderName string) downloaders.AddEvidence {
	return downloaders.AddEvidence{
		DownloaderName: downloaderName, Adapter: "qbittorrent", ConfigurationSHA256: strings.Repeat("d", 64),
		TorrentBytes: len(torrent), TorrentSHA256: sha256String(torrent), Result: qbittorrent.AddResult{Hashes: inspection.Hashes},
		Observed: &downloaders.TorrentEvidence{
			DownloaderName: downloaderName, Adapter: "qbittorrent", ConfigurationSHA256: strings.Repeat("d", 64),
			Torrent: qbittorrent.Torrent{
				Hash: inspection.Hashes.V1SHA1, State: "checkingUP", Progress: 0.5, TotalSize: inspection.TotalSizeBytes,
				SavePath: "/remote/downloads", ContentPath: "/remote/downloads/video.mkv", Category: "MTEAM", Tags: "retorrent,mteam",
			},
		},
	}
}
