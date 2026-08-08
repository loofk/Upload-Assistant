package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/media"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type MediaInspector interface {
	Inspect(context.Context, string) (media.Inspection, error)
}

func WithMediaInfo(inspector MediaInspector, artifactStore ArtifactWriter) Option {
	return func(runner *Runner) {
		runner.executors["media_info"] = mediaInfoExecutor{
			inspector: inspector, artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type mediaInfoExecutor struct {
	inspector MediaInspector
	artifacts ArtifactWriter
	recorder  ArtifactRecorder
}

func (executor mediaInfoExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.inspector == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("media inspection dependencies are unavailable")
	}
	var snapshot struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		return nil, invalidSnapshotBlock(fmt.Errorf("decode media info step snapshot: %w", err))
	}
	var content struct {
		LocalRoot       string   `json:"local_root"`
		MediaCandidates []string `json:"media_candidates"`
		ManifestID      string   `json:"manifest_artifact_id"`
		ManifestSHA256  string   `json:"manifest_sha256"`
	}
	body, exists := snapshot.PreviousSteps["content_resolve"]
	if !exists || json.Unmarshal(body, &content) != nil || content.LocalRoot == "" {
		return nil, invalidSnapshotBlock(fmt.Errorf("content_resolve media evidence is missing or incomplete"))
	}
	if requiresDiscInfo(content.MediaCandidates) {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: "bdinfo_adapter_required", Message: "Blu-ray/DVD structures require a configured BDInfo-compatible adapter; MediaInfo substitution is not accepted"}},
			NextActions: []NextAction{{Action: "configure_bdinfo_adapter", Description: "Configure and validate the BDInfo adapter, then resume this step."}},
			ResumeState: map[string]any{"media_info": map[string]any{"content_root": content.LocalRoot, "required_tool": "bdinfo"}},
		}
	}
	selected, selectedSize, err := selectLargestMedia(content.MediaCandidates)
	if err != nil {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: "media_candidate_required", Message: err.Error()}},
			NextActions: []NextAction{{Action: "review_content_manifest", Description: "Provide a supported video file or configure a disc information adapter."}},
			ResumeState: map[string]any{"media_info": map[string]any{"content_root": content.LocalRoot, "candidate_count": len(content.MediaCandidates)}},
		}
	}
	inspection, err := executor.inspector.Inspect(ctx, selected)
	if err != nil {
		code, action := "media_inspection_failed", "retry_media_inspection"
		description := "Review the media file and retry this independently resumable step."
		if errors.Is(err, media.ErrToolUnavailable) {
			code, action = "mediainfo_tool_unavailable", "configure_mediainfo_tool"
			description = "Install or configure the MediaInfo binary before resuming."
		}
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: code, Message: err.Error()}},
			NextActions: []NextAction{{Action: action, Description: description}},
			ResumeState: map[string]any{"media_info": map[string]any{"selected_path": selected, "selected_size_bytes": selectedSize}},
		}
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, "mediainfo.json", bytes.NewReader(inspection.Document))
	if err != nil {
		return nil, fmt.Errorf("persist MediaInfo artifact: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "mediainfo", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"tool": inspection.Tool, "version": inspection.Version,
			"input_path": selected, "input_size_bytes": selectedSize,
			"selection": "largest_media_candidate", "candidate_count": len(content.MediaCandidates),
			"content_manifest_artifact_id": content.ManifestID,
			"content_manifest_sha256":      content.ManifestSHA256,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register MediaInfo artifact: %w", err)
	}
	return mustJSON(map[string]any{
		"kind": "mediainfo", "tool": inspection.Tool, "version": inspection.Version,
		"selected_path": selected, "selected_size_bytes": selectedSize,
		"selection": "largest_media_candidate", "candidate_count": len(content.MediaCandidates),
		"artifact_id": recorded.ID, "artifact_sha256": recorded.SHA256,
		"artifact_storage_path": recorded.StoragePath, "duration_ms": inspection.DurationMS,
	}), nil
}

func selectLargestMedia(candidates []string) (string, int64, error) {
	unique := make([]string, 0, len(candidates))
	seen := map[string]struct{}{}
	for _, candidate := range candidates {
		candidate = filepath.Clean(strings.TrimSpace(candidate))
		if !filepath.IsAbs(candidate) {
			continue
		}
		if _, exists := seen[candidate]; !exists {
			seen[candidate] = struct{}{}
			unique = append(unique, candidate)
		}
	}
	slices.Sort(unique)
	selected := ""
	var selectedSize int64 = -1
	for _, candidate := range unique {
		info, err := os.Stat(candidate)
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		if info.Size() > selectedSize {
			selected, selectedSize = candidate, info.Size()
		}
	}
	if selected == "" {
		return "", 0, fmt.Errorf("no readable media candidate is available")
	}
	return selected, selectedSize, nil
}

func requiresDiscInfo(candidates []string) bool {
	for _, candidate := range candidates {
		normalized := strings.ToUpper(filepath.ToSlash(candidate))
		if strings.Contains(normalized, "/BDMV/STREAM/") || strings.Contains(normalized, "/VIDEO_TS/") {
			return true
		}
	}
	return false
}
