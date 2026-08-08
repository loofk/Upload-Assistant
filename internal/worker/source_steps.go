package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const artifactRetention = 30 * 24 * time.Hour

type SourceProvider interface {
	Inspect(context.Context, sites.SourceReference) (sites.SourceInfo, error)
	Download(context.Context, sites.SourceReference) (sites.DownloadedTorrent, error)
}

type ArtifactWriter interface {
	Write(context.Context, artifacts.Scope, string, io.Reader) (artifacts.File, error)
}

type ArtifactRecorder interface {
	RegisterArtifact(context.Context, workflow.RegisterArtifactInput) (workflow.Artifact, error)
}

func WithSourceAdapters(provider SourceProvider, artifactStore ArtifactWriter) Option {
	return func(runner *Runner) {
		runner.executors["source_inspect"] = sourceInspectExecutor{provider: provider}
		runner.executors["source_torrent"] = sourceTorrentExecutor{
			provider: provider, artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type sourceInspectExecutor struct{ provider SourceProvider }

func (executor sourceInspectExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil {
		return nil, fmt.Errorf("source adapter registry is unavailable")
	}
	reference, err := sourceReferenceFromSnapshot(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	info, err := executor.provider.Inspect(ctx, reference)
	if err != nil {
		return nil, sourceAdapterBlock(err, reference, "inspect_source")
	}
	return mustJSON(map[string]any{"source_info": info}), nil
}

type sourceTorrentExecutor struct {
	provider  SourceProvider
	artifacts ArtifactWriter
	recorder  ArtifactRecorder
}

func (executor sourceTorrentExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("source torrent artifact dependencies are unavailable")
	}
	reference, err := sourceReferenceFromSnapshot(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	download, err := executor.provider.Download(ctx, reference)
	if err != nil {
		return nil, sourceAdapterBlock(err, reference, "download_source_torrent")
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, download.Filename, bytes.NewReader(download.Bytes))
	if err != nil {
		return nil, fmt.Errorf("persist source torrent artifact: %w", err)
	}
	if file.SHA256 != download.SHA256 || file.SizeBytes != download.SizeBytes {
		return nil, fmt.Errorf("source torrent artifact evidence mismatch")
	}
	metadata := mustJSON(map[string]any{
		"tracker": reference.Tracker, "torrent_id": reference.TorrentID,
		"v1_infohash": download.Hashes.V1SHA1, "v2_infohash": download.Hashes.V2SHA256,
	})
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "source_torrent", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: download.ContentType, SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: metadata, Retention: artifactRetention,
		Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register source torrent artifact: %w", err)
	}
	return mustJSON(map[string]any{
		"artifact_id": recorded.ID, "storage_path": recorded.StoragePath,
		"filename": recorded.Filename, "size_bytes": recorded.SizeBytes,
		"sha256": recorded.SHA256, "hashes": download.Hashes,
		"tracker": reference.Tracker, "torrent_id": reference.TorrentID,
	}), nil
}

type frozenStepInputs struct {
	PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
}

func sourceReferenceFromSnapshot(snapshot json.RawMessage) (sites.SourceReference, error) {
	var frozen frozenStepInputs
	if err := json.Unmarshal(snapshot, &frozen); err != nil {
		return sites.SourceReference{}, fmt.Errorf("decode frozen step input: %w", err)
	}
	previous, exists := frozen.PreviousSteps["source_parse"]
	if !exists {
		return sites.SourceReference{}, fmt.Errorf("source_parse output is missing")
	}
	var parsed struct {
		Source sites.SourceReference `json:"source"`
	}
	if err := json.Unmarshal(previous, &parsed); err != nil {
		return sites.SourceReference{}, fmt.Errorf("decode source_parse output: %w", err)
	}
	if parsed.Source.Tracker == "" || parsed.Source.TorrentID == "" {
		return sites.SourceReference{}, fmt.Errorf("source_parse output is incomplete")
	}
	return parsed.Source, nil
}

func invalidSnapshotBlock(err error) *BlockError {
	return &BlockError{
		Code: "step_input_snapshot_invalid", Message: err.Error(),
		NextActions: []NextAction{{Action: "restart_job", Description: "Create a new job so immutable prior-step evidence can be rebuilt."}},
		ResumeState: map[string]any{},
	}
}

func sourceAdapterBlock(err error, reference sites.SourceReference, operation string) *BlockError {
	code, message, temporary := sites.ErrorDetails(err)
	action := "configure_site_credentials"
	description := "Configure or refresh the encrypted source-site cookie/passkey and resume this step."
	switch code {
	case "site_adapter_unavailable", "site_adapter_mismatch":
		action = "install_site_adapter"
		description = "Implement and enable the source-site adapter before resuming."
	case "source_torrent_not_found", "source_reference_invalid", "source_reference_mismatch":
		action = "verify_source_reference"
		description = "Verify that the source link and torrent id still identify an accessible torrent."
	case "source_request_failed", "source_site_unavailable":
		action = "retry_step"
		description = "Verify network access to the source site, then resume or retry this step."
	}
	return &BlockError{
		Blockers: []Blocker{{Code: code, Message: message, SiteCode: reference.Tracker}},
		NextActions: []NextAction{{Action: action, Description: description, Parameters: map[string]any{
			"site_code": reference.Tracker, "operation": operation,
		}}},
		ResumeState: map[string]any{
			"source":    map[string]any{"tracker": reference.Tracker, "torrent_id": reference.TorrentID},
			"retryable": temporary, "retry_step": operation,
		},
	}
}
