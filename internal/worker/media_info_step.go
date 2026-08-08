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
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/media"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type MediaInspector interface {
	Inspect(context.Context, string) (media.Inspection, error)
}

func WithMediaInfo(inspector MediaInspector, artifactStore ArtifactWriter) Option {
	return WithMediaInspection(inspector, nil, artifactStore)
}

func WithMediaInspection(mediaInspector, discInspector MediaInspector, artifactStore ArtifactWriter) Option {
	return func(runner *Runner) {
		runner.executors["media_info"] = mediaInfoExecutor{
			mediaInspector: mediaInspector, discInspector: discInspector,
			artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type mediaInfoExecutor struct {
	mediaInspector MediaInspector
	discInspector  MediaInspector
	artifacts      ArtifactWriter
	recorder       ArtifactRecorder
}

func (executor mediaInfoExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.artifacts == nil || executor.recorder == nil {
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
		TotalSizeBytes  int64    `json:"total_size_bytes"`
		ManifestID      string   `json:"manifest_artifact_id"`
		ManifestSHA256  string   `json:"manifest_sha256"`
	}
	body, exists := snapshot.PreviousSteps["content_resolve"]
	if !exists || json.Unmarshal(body, &content) != nil || content.LocalRoot == "" {
		return nil, invalidSnapshotBlock(fmt.Errorf("content_resolve media evidence is missing or incomplete"))
	}
	discKind, discRoot, err := detectDiscStructure(content.MediaCandidates)
	if err != nil {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: "disc_structure_ambiguous", Message: err.Error()}},
			NextActions: []NextAction{{Action: "review_content_manifest", Description: "Review the resolved disc paths, then resume this step."}},
			ResumeState: map[string]any{"media_info": map[string]any{"content_root": content.LocalRoot, "candidate_count": len(content.MediaCandidates)}},
		}
	}
	if discKind == "dvd" {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: "dvdinfo_adapter_required", Message: "DVD VIDEO_TS structures require a dedicated audited DVD information adapter; BDInfo and MediaInfo substitution are not accepted"}},
			NextActions: []NextAction{{Action: "configure_dvdinfo_adapter", Description: "Configure and validate a DVD information adapter, then resume this step."}},
			ResumeState: map[string]any{"media_info": map[string]any{"content_root": content.LocalRoot, "disc_root": discRoot, "required_tool": "dvdinfo"}},
		}
	}

	inspectionKind := "mediainfo"
	selected, selectedSize := "", int64(0)
	inspector := executor.mediaInspector
	if discKind == "bluray" {
		inspectionKind, selected, selectedSize, inspector = "bdinfo", discRoot, content.TotalSizeBytes, executor.discInspector
	} else {
		selected, selectedSize, err = selectLargestMedia(content.MediaCandidates)
	}
	if err != nil {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: "media_candidate_required", Message: err.Error()}},
			NextActions: []NextAction{{Action: "review_content_manifest", Description: "Provide a supported video file or configure a disc information adapter."}},
			ResumeState: map[string]any{"media_info": map[string]any{"content_root": content.LocalRoot, "candidate_count": len(content.MediaCandidates)}},
		}
	}
	if inspector == nil {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: inspectionKind + "_adapter_required", Message: inspectionKind + " adapter is not configured; substitution is not accepted"}},
			NextActions: []NextAction{{Action: "configure_" + inspectionKind + "_adapter", Description: "Configure and validate the required media inspection adapter, then resume this step."}},
			ResumeState: map[string]any{"media_info": map[string]any{"selected_path": selected, "selected_size_bytes": selectedSize, "required_tool": inspectionKind}},
		}
	}
	inspection, err := inspector.Inspect(ctx, selected)
	if err != nil {
		code, action := "media_inspection_failed", "retry_media_inspection"
		description := "Review the media file and retry this independently resumable step."
		if errors.Is(err, media.ErrToolUnavailable) {
			code, action = inspectionKind+"_tool_unavailable", "configure_"+inspectionKind+"_tool"
			description = "Install or configure the required media inspection binary before resuming."
		}
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: code, Message: err.Error()}},
			NextActions: []NextAction{{Action: action, Description: description}},
			ResumeState: map[string]any{"media_info": map[string]any{"selected_path": selected, "selected_size_bytes": selectedSize}},
		}
	}
	if inspection.Tool != inspectionKind || len(inspection.Document) == 0 ||
		(inspectionKind == "mediainfo" && !json.Valid(inspection.Document)) ||
		(inspectionKind == "bdinfo" && (!utf8.Valid(inspection.Document) || bytes.IndexByte(inspection.Document, 0) >= 0)) {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: "media_inspection_output_invalid", Message: "media inspection output does not match the required audited format"}},
			NextActions: []NextAction{{Action: "review_media_inspection_adapter", Description: "Review the configured tool output and retry this step."}},
			ResumeState: map[string]any{"media_info": map[string]any{"selected_path": selected, "required_tool": inspectionKind}},
		}
	}
	filename, mimeType := inspection.Filename, inspection.MIMEType
	if inspectionKind == "mediainfo" {
		filename, mimeType = "mediainfo.json", "application/json"
	} else {
		filename, mimeType = "bdinfo.txt", "text/plain; charset=utf-8"
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, filename, bytes.NewReader(inspection.Document))
	if err != nil {
		return nil, fmt.Errorf("persist media inspection artifact: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: inspectionKind, StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: mimeType, SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"tool": inspection.Tool, "version": inspection.Version,
			"input_path": selected, "input_size_bytes": selectedSize,
			"selection":       map[bool]string{true: "bluray_disc_root", false: "largest_media_candidate"}[discKind == "bluray"],
			"candidate_count": len(content.MediaCandidates), "document_format": inspection.Format,
			"content_manifest_artifact_id": content.ManifestID,
			"content_manifest_sha256":      content.ManifestSHA256,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register media inspection artifact: %w", err)
	}
	return mustJSON(map[string]any{
		"kind": inspectionKind, "tool": inspection.Tool, "version": inspection.Version, "document_format": inspection.Format,
		"selected_path": selected, "selected_size_bytes": selectedSize,
		"selection": map[bool]string{true: "bluray_disc_root", false: "largest_media_candidate"}[discKind == "bluray"], "candidate_count": len(content.MediaCandidates),
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

func detectDiscStructure(candidates []string) (string, string, error) {
	roots := map[string]string{}
	for _, candidate := range candidates {
		cleaned := filepath.Clean(strings.TrimSpace(candidate))
		if !filepath.IsAbs(cleaned) {
			continue
		}
		normalized := strings.ToUpper(filepath.ToSlash(cleaned))
		for marker, kind := range map[string]string{"/BDMV/STREAM/": "bluray", "/VIDEO_TS/": "dvd"} {
			if index := strings.Index(normalized, marker); index >= 0 {
				root := filepath.Clean(filepath.FromSlash(filepath.ToSlash(cleaned)[:index]))
				roots[kind+"\x00"+root] = root
			}
		}
	}
	if len(roots) == 0 {
		return "", "", nil
	}
	if len(roots) != 1 {
		return "", "", fmt.Errorf("multiple or mixed disc structures are present")
	}
	for key, root := range roots {
		kind, _, _ := strings.Cut(key, "\x00")
		return kind, root, nil
	}
	return "", "", nil
}
