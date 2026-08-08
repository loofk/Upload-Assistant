package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeSummaryCatalog struct {
	artifacts []workflow.Artifact
	inputs    []workflow.RegisterArtifactInput
}

func (catalog *fakeSummaryCatalog) ListArtifacts(context.Context, string) ([]workflow.Artifact, error) {
	return append([]workflow.Artifact(nil), catalog.artifacts...), nil
}

func (catalog *fakeSummaryCatalog) RegisterArtifact(_ context.Context, input workflow.RegisterArtifactInput) (workflow.Artifact, error) {
	catalog.inputs = append(catalog.inputs, input)
	return workflow.Artifact{
		ID: "summary-artifact", JobID: input.JobID, StepID: input.StepID, AttemptID: input.AttemptID,
		Kind: input.Kind, StorageBackend: "local", StoragePath: input.StoragePath,
		Filename: input.Filename, MIMEType: input.MIMEType, SizeBytes: input.SizeBytes, SHA256: input.SHA256,
	}, nil
}

func TestSummaryStepCompletesWithBoundEvidenceAndNoSourceSecret(t *testing.T) {
	execution, store, catalog := summaryExecution(t)
	output, err := (summaryExecutor{
		artifacts: store, catalog: catalog, now: func() time.Time { return time.Unix(20_000, 0) },
	}).Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if len(catalog.inputs) != 1 || catalog.inputs[0].Kind != "job_summary" {
		t.Fatalf("summary artifact inputs = %#v", catalog.inputs)
	}
	var result struct {
		OK          bool            `json:"ok"`
		Status      string          `json:"status"`
		JobID       string          `json:"job_id"`
		SummaryFile summaryArtifact `json:"summary_file"`
	}
	if json.Unmarshal(output, &result) != nil || !result.OK || result.Status != "complete" || result.JobID != "job-id" ||
		result.SummaryFile.ArtifactID != "summary-artifact" || result.SummaryFile.SHA256 == "" {
		t.Fatalf("summary output = %s", output)
	}
	file, err := store.Open(catalog.inputs[0].StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(file)
	file.Close()
	for _, secret := range []string{"super-secret-passkey", "requested_source", "announce/secret-passkey"} {
		if bytes.Contains(output, []byte(secret)) || bytes.Contains(body, []byte(secret)) {
			t.Fatalf("summary exposed %q", secret)
		}
	}
}

func TestSummaryStepBlocksWhenRequiredArtifactIsMissing(t *testing.T) {
	execution, store, catalog := summaryExecution(t)
	catalog.artifacts = catalog.artifacts[:len(catalog.artifacts)-1]
	_, err := (summaryExecutor{artifacts: store, catalog: catalog}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "summary_evidence_incomplete" || len(catalog.inputs) != 0 {
		t.Fatalf("summary missing artifact blocker/inputs = %#v/%#v", blocked, catalog.inputs)
	}
}

func summaryExecution(t *testing.T) (Execution, WorkflowArtifactStore, *fakeSummaryCatalog) {
	t.Helper()
	seedExecution, store, evidence, files := targetSeedExecution(t)
	seedOutput, err := (targetSeedVerifyExecutor{
		provider: &fakeDownloaderProvider{inspection: evidence, files: files}, artifacts: store,
		recorder: &sequenceArtifactRecorder{}, now: func() time.Time { return time.Unix(10_000, 0) },
	}).Execute(context.Background(), seedExecution)
	if err != nil {
		t.Fatal(err)
	}
	var frozen struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(seedExecution.Step.InputSnapshot, &frozen); err != nil {
		t.Fatal(err)
	}
	frozen.PreviousSteps["target_seed_verify"] = seedOutput
	frozen.PreviousSteps["source_parse"] = mustJSON(map[string]any{
		"source": sites.SourceReference{
			Tracker: "U2", TorrentID: "60635",
			RequestedSource: "https://u2.dmhy.org/download.php?id=60635&passkey=super-secret-passkey",
			DetailsURL:      "https://u2.dmhy.org/download.php?id=60635&passkey=super-secret-passkey",
		},
		"target": "MTEAM",
	})
	frozen.PreviousSteps["source_inspect"] = mustJSON(map[string]any{"source_info": sites.SourceInfo{
		Tracker: "U2", TorrentID: "60635", Name: "Fixture Release", IMDbID: "tt1234567", RetrievedAt: time.Unix(1, 0).UTC(),
	}})
	frozen.PreviousSteps["source_rules"] = mustJSON(summaryRule{
		SiteCode: "U2", Role: "source", RevisionID: "source-rule", Fingerprint: strings.Repeat("1", 64),
		Accepted: true, AcceptanceSHA: strings.Repeat("2", 64),
	})
	frozen.PreviousSteps["downloader_add"] = mustJSON(map[string]any{
		"downloader_name": "box", "torrent_hash": strings.Repeat("a", 40),
		"limits": map[string]any{"applied_download": 1}, "options": map[string]any{"save_path": "/remote/downloads"},
	})
	frozen.PreviousSteps["downloader_wait"] = mustJSON(map[string]any{"completed": true, "downloader_name": "box", "torrent_hash": strings.Repeat("a", 40)})
	frozen.PreviousSteps["metadata"] = mustJSON(map[string]any{
		"identity": map[string]any{"title": "Fixture", "imdb_id": "tt1234567"},
		"links":    map[string]any{"imdb": "https://www.imdb.com/title/tt1234567/"}, "identity_strength": "strong",
		"metadata_artifact_id": "metadata-artifact", "metadata_sha256": strings.Repeat("3", 64), "metadata_storage_path": "metadata.json",
	})
	frozen.PreviousSteps["media_info"] = mustJSON(map[string]any{
		"kind": "mediainfo", "tool": "mediainfo", "version": "fixture", "selected_path": "/downloads/video.mkv",
		"artifact_id": "mediainfo-artifact", "artifact_sha256": strings.Repeat("4", 64), "artifact_storage_path": "mediainfo.json",
	})
	frozen.PreviousSteps["screenshots"] = mustJSON(map[string]any{
		"generated": true, "screenshot_count": 1, "profile": map[string]any{"name": "default", "revision": 1},
		"artifacts": []screenshotArtifactInput{{
			Index: 1, ArtifactID: "screenshot-artifact", SHA256: strings.Repeat("5", 64), StoragePath: "shot.png", SizeBytes: 10, MIMEType: "image/png",
		}},
	})
	frozen.PreviousSteps["image_upload"] = mustJSON(map[string]any{
		"uploaded": true, "image_count": 1, "receipts": []imageUploadReceipt{{
			Index: 1, ReceiptID: "image-receipt", ReceiptSHA: strings.Repeat("6", 64),
		}},
	})
	patchSummaryArtifactIDs(t, frozen.PreviousSteps)
	snapshot := mustJSON(map[string]any{"previous_steps": frozen.PreviousSteps})
	bindings, err := decodeSummaryBindings(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	catalog := &fakeSummaryCatalog{artifacts: summaryFixtureArtifacts(bindings)}
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "summary-step", InputSnapshot: snapshot},
		Attempt: workflow.Attempt{ID: "summary-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "fixture"},
	}, store, catalog
}

func patchSummaryArtifactIDs(t *testing.T, previous map[string]json.RawMessage) {
	t.Helper()
	patch := func(step string, values map[string]any) {
		var document map[string]any
		if err := json.Unmarshal(previous[step], &document); err != nil {
			t.Fatal(err)
		}
		for key, value := range values {
			document[key] = value
		}
		previous[step] = mustJSON(document)
	}
	patch("target_torrent", map[string]any{"target_torrent_artifact_id": "target-torrent", "receipt_artifact_id": "target-torrent-receipt"})
	patch("target_upload", map[string]any{"preupload_duplicate_check_artifact_id": "fresh-dupe", "upload_receipt_artifact_id": "upload-receipt"})
	patch("target_torrent_download", map[string]any{"target_torrent_artifact_id": "downloaded-torrent", "receipt_artifact_id": "download-receipt"})
	patch("target_inject", map[string]any{"receipt_artifact_id": "inject-receipt"})
	patch("target_seed_verify", map[string]any{"observation_artifact_id": "seed-observation"})
}

func summaryFixtureArtifacts(bindings summaryBindings) []workflow.Artifact {
	items := []struct{ id, kind, sha, path string }{
		{bindings.SourceTorrent.ArtifactID, "source_torrent", bindings.SourceTorrent.SHA256, bindings.SourceTorrent.StoragePath},
		{bindings.Content.ArtifactID, "content_manifest", bindings.Content.SHA256, bindings.Content.StoragePath},
		{bindings.Metadata.ArtifactID, "metadata", bindings.Metadata.SHA256, bindings.Metadata.StoragePath},
		{bindings.MediaInfo.ArtifactID, "mediainfo", bindings.MediaInfo.SHA256, bindings.MediaInfo.StoragePath},
		{bindings.Screenshots.Artifacts[0].ArtifactID, "screenshot", bindings.Screenshots.Artifacts[0].SHA256, bindings.Screenshots.Artifacts[0].StoragePath},
		{bindings.Images.Receipts[0].ReceiptID, "image_upload_receipt", bindings.Images.Receipts[0].ReceiptSHA, "image-receipt.json"},
		{bindings.Package.ArtifactID, "target_package", bindings.Package.SHA256, bindings.Package.StoragePath},
		{bindings.Duplicate.ArtifactID, "duplicate_check", bindings.Duplicate.SHA256, bindings.Duplicate.StoragePath},
		{bindings.TargetTorrent.ArtifactID, "target_torrent", bindings.TargetTorrent.SHA256, bindings.TargetTorrent.StoragePath},
		{bindings.TargetTorrent.ReceiptID, "target_torrent_receipt", bindings.TargetTorrent.ReceiptSHA, bindings.TargetTorrent.ReceiptPath},
		{bindings.Upload.FreshDupeID, "preupload_duplicate_check", bindings.Upload.FreshDupeSHA, "fresh-dupe.json"},
		{bindings.Upload.ReceiptID, "target_upload_receipt", bindings.Upload.ReceiptSHA, bindings.Upload.ReceiptPath},
		{bindings.Downloaded.ArtifactID, "target_downloaded_torrent", bindings.Downloaded.SHA256, bindings.Downloaded.StoragePath},
		{bindings.Downloaded.ReceiptID, "target_torrent_download_receipt", bindings.Downloaded.ReceiptSHA, bindings.Downloaded.ReceiptPath},
		{bindings.Injection.ReceiptID, "target_injection_receipt", bindings.Injection.ReceiptSHA, bindings.Injection.ReceiptPath},
		{bindings.Seed.ObservationID, "target_seed_observation", bindings.Seed.ObservationSHA, bindings.Seed.ObservationPath},
	}
	result := make([]workflow.Artifact, 0, len(items))
	for _, item := range items {
		result = append(result, workflow.Artifact{
			ID: item.id, JobID: "job-id", Kind: item.kind, StorageBackend: "local", StoragePath: item.path,
			MIMEType: "application/json", SizeBytes: 10, SHA256: item.sha,
		})
	}
	return result
}
