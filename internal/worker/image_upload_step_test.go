package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/imagehosts"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeImageHostProvider struct {
	snapshot imagehosts.HostSnapshot
	result   imagehosts.UploadEvidence
	err      error
	uploads  int
}

func (provider *fakeImageHostProvider) Snapshot(context.Context, string) (imagehosts.HostSnapshot, error) {
	return provider.snapshot, provider.err
}

func (provider *fakeImageHostProvider) Upload(_ context.Context, _ string, _ imagehosts.Image, _ workflow.Actor) (imagehosts.UploadEvidence, error) {
	provider.uploads++
	return provider.result, provider.err
}

type fakeArtifactCatalog struct{ artifacts []workflow.Artifact }

func (catalog *fakeArtifactCatalog) RegisterArtifact(_ context.Context, input workflow.RegisterArtifactInput) (workflow.Artifact, error) {
	artifact := workflow.Artifact{
		ID: "receipt-id", JobID: input.JobID, StepID: input.StepID, AttemptID: input.AttemptID,
		Kind: input.Kind, StorageBackend: "local", StoragePath: input.StoragePath,
		Filename: input.Filename, MIMEType: input.MIMEType, SizeBytes: input.SizeBytes,
		SHA256: input.SHA256, Metadata: input.Metadata,
	}
	catalog.artifacts = append(catalog.artifacts, artifact)
	return artifact, nil
}

func (catalog *fakeArtifactCatalog) ListArtifacts(context.Context, string) ([]workflow.Artifact, error) {
	return append([]workflow.Artifact(nil), catalog.artifacts...), nil
}

func TestImageUploadStepPersistsAndReusesPerImageReceipt(t *testing.T) {
	store := mustArtifactStore(t)
	screenshotBytes := []byte("\x89PNG\r\n\x1a\nfixture")
	written, err := store.Write(context.Background(), artifacts.Scope{
		JobID: "job-id", StepID: "screenshots-step", AttemptID: "screenshots-attempt",
	}, "screenshot-01.png", bytes.NewReader(screenshotBytes))
	if err != nil {
		t.Fatal(err)
	}
	host := imagehosts.HostSnapshot{
		ID: "host-id", Name: "primary", Adapter: "imgbb",
		ConfigSHA256: "config-sha", ConfigurationTime: time.Unix(1, 0).UTC(),
	}
	provider := &fakeImageHostProvider{snapshot: host, result: imagehosts.UploadEvidence{
		ImageHostID: host.ID, ImageHostName: host.Name, Adapter: host.Adapter,
		ConfigSHA256: host.ConfigSHA256, ConfigurationTime: host.ConfigurationTime,
		SourceFilename: written.Filename, SourceMIMEType: "image/png",
		SourceSizeBytes: written.SizeBytes, SourceSHA256: written.SHA256,
		Result: imagehosts.UploadResult{URL: "https://i.ibb.co/path/image.png", ViewerURL: "https://ibb.co/id"},
	}}
	catalog := &fakeArtifactCatalog{}
	executor := imageUploadExecutor{provider: provider, artifacts: store, catalog: catalog}
	execution := imageUploadExecution(written)
	output, err := executor.Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if provider.uploads != 1 || len(catalog.artifacts) != 1 || catalog.artifacts[0].Kind != "image_upload_receipt" {
		t.Fatalf("upload count/artifacts = %d/%#v", provider.uploads, catalog.artifacts)
	}
	var result struct {
		Uploaded   bool                 `json:"uploaded"`
		ImageCount int                  `json:"image_count"`
		Receipts   []imageUploadReceipt `json:"receipts"`
	}
	if err := json.Unmarshal(output, &result); err != nil || !result.Uploaded || result.ImageCount != 1 || result.Receipts[0].Reused {
		t.Fatalf("first upload output/error = %#v/%v", result, err)
	}

	execution.Attempt.ID = "second-attempt"
	output, err = executor.Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if provider.uploads != 1 {
		t.Fatalf("retry performed %d remote uploads, want 1", provider.uploads)
	}
	if err := json.Unmarshal(output, &result); err != nil || !result.Receipts[0].Reused {
		t.Fatalf("reused output/error = %#v/%v", result, err)
	}

	provider.snapshot.ConfigurationTime = provider.snapshot.ConfigurationTime.Add(time.Second)
	provider.result.ConfigurationTime = provider.snapshot.ConfigurationTime
	execution.Attempt.ID = "rotated-configuration-attempt"
	if _, err := executor.Execute(context.Background(), execution); err != nil {
		t.Fatal(err)
	}
	if provider.uploads != 2 || len(catalog.artifacts) != 2 {
		t.Fatalf("configuration rotation uploads/artifacts = %d/%d, want 2/2", provider.uploads, len(catalog.artifacts))
	}
}

func TestImageUploadStepBlocksConfigurationRaceWithoutReceipt(t *testing.T) {
	store := mustArtifactStore(t)
	written, err := store.Write(context.Background(), artifacts.Scope{
		JobID: "job-id", StepID: "screenshots-step", AttemptID: "screenshots-attempt",
	}, "screenshot-01.png", bytes.NewReader([]byte("\x89PNG\r\n\x1a\nfixture")))
	if err != nil {
		t.Fatal(err)
	}
	host := imagehosts.HostSnapshot{
		ID: "host-id", Name: "primary", Adapter: "imgbb",
		ConfigSHA256: "config-sha", ConfigurationTime: time.Unix(1, 0).UTC(),
	}
	provider := &fakeImageHostProvider{snapshot: host, result: imagehosts.UploadEvidence{
		ImageHostID: host.ID, ImageHostName: host.Name, Adapter: host.Adapter,
		ConfigSHA256: host.ConfigSHA256, ConfigurationTime: host.ConfigurationTime.Add(time.Second),
		Result: imagehosts.UploadResult{URL: "https://i.ibb.co/path/image.png"},
	}}
	catalog := &fakeArtifactCatalog{}
	_, err = (imageUploadExecutor{provider: provider, artifacts: store, catalog: catalog}).Execute(
		context.Background(), imageUploadExecution(written),
	)
	var blocked *BlockError
	if !errors.As(err, &blocked) || len(blocked.Blockers) != 1 || blocked.Blockers[0].Code != "image_host_configuration_changed" {
		t.Fatalf("configuration race error = %#v", err)
	}
	if len(catalog.artifacts) != 0 {
		t.Fatalf("configuration race registered artifacts = %#v", catalog.artifacts)
	}
}

func imageUploadExecution(file artifacts.File) Execution {
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "image-step", InputSnapshot: mustJSON(map[string]any{
			"job_input": map[string]any{"image_host": map[string]any{"name": "primary"}},
			"previous_steps": map[string]any{"screenshots": map[string]any{
				"generated": true,
				"artifacts": []map[string]any{{
					"index": 1, "timestamp_seconds": 50, "artifact_id": "screenshot-id",
					"filename": file.Filename, "mime_type": "image/png", "size_bytes": file.SizeBytes,
					"sha256": file.SHA256, "storage_path": file.RelativePath,
				}},
			}},
		})},
		Attempt: workflow.Attempt{ID: "image-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "test-worker"},
	}
}
