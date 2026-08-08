package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/media"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type ScreenshotProfileProvider interface {
	GetRuntimeScreenshotProfile(context.Context, string) (integrations.RuntimeScreenshotProfile, error)
}

type ScreenshotGenerator interface {
	Generate(context.Context, string, integrations.ScreenshotConfig) (media.ScreenshotBatch, error)
}

func WithScreenshots(provider ScreenshotProfileProvider, generator ScreenshotGenerator, artifactStore ArtifactWriter) Option {
	return func(runner *Runner) {
		runner.executors["screenshots"] = screenshotsExecutor{
			profiles: provider, generator: generator, artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type screenshotsExecutor struct {
	profiles  ScreenshotProfileProvider
	generator ScreenshotGenerator
	artifacts ArtifactWriter
	recorder  ArtifactRecorder
}

func (executor screenshotsExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.profiles == nil || executor.generator == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("screenshot workflow dependencies are unavailable")
	}
	profileName, inputPath, err := screenshotInputs(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	profile, err := executor.profiles.GetRuntimeScreenshotProfile(ctx, profileName)
	if err != nil {
		code := "screenshot_profile_unavailable"
		if errors.Is(err, integrations.ErrNotFound) {
			code = "screenshot_profile_required"
		}
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: code, Message: fmt.Sprintf("screenshot profile %q is unavailable", profileName)}},
			NextActions: []NextAction{{Action: "configure_screenshot_profile", Description: "Create and enable an immutable screenshot profile revision before resuming.", Parameters: map[string]any{"name": profileName}}},
			ResumeState: map[string]any{"screenshots": map[string]any{"profile": profileName}},
		}
	}
	batch, err := executor.generator.Generate(ctx, inputPath, profile.ScreenshotConfig)
	if err != nil {
		code, action := "screenshot_generation_failed", "retry_screenshots"
		if errors.Is(err, media.ErrToolUnavailable) {
			code, action = "screenshot_tool_unavailable", "configure_screenshot_tools"
		}
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: code, Message: err.Error()}},
			NextActions: []NextAction{{Action: action, Description: "Correct the screenshot tool/profile configuration and resume this step."}},
			ResumeState: map[string]any{"screenshots": map[string]any{"profile": profileName, "input_path": inputPath}},
		}
	}
	configHash := sha256.Sum256(profile.Config)
	artifactOutputs := make([]map[string]any, 0, len(batch.Screenshots))
	for _, screenshot := range batch.Screenshots {
		file, err := executor.artifacts.Write(ctx, artifacts.Scope{
			JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		}, screenshot.Filename, bytes.NewReader(screenshot.Bytes))
		if err != nil {
			return nil, fmt.Errorf("persist screenshot %d: %w", screenshot.Index, err)
		}
		if file.SizeBytes != screenshot.SizeBytes {
			return nil, fmt.Errorf("screenshot %d artifact size mismatch", screenshot.Index)
		}
		recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
			JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
			Kind: "screenshot", StoragePath: file.RelativePath, Filename: file.Filename,
			MIMEType: screenshot.MIMEType, SizeBytes: file.SizeBytes, SHA256: file.SHA256,
			Metadata: mustJSON(map[string]any{
				"index": screenshot.Index, "timestamp_seconds": screenshot.Timestamp,
				"format": screenshot.Format, "profile_id": profile.ID,
				"profile_name": profile.Name, "profile_revision": profile.Revision,
				"profile_config_sha256": hex.EncodeToString(configHash[:]),
				"tool":                  batch.Tool, "version": batch.Version, "input_path": inputPath,
			}),
			Retention: artifactRetention, Actor: execution.Actor,
		})
		if err != nil {
			return nil, fmt.Errorf("register screenshot %d artifact: %w", screenshot.Index, err)
		}
		artifactOutputs = append(artifactOutputs, map[string]any{
			"index": screenshot.Index, "timestamp_seconds": screenshot.Timestamp,
			"artifact_id": recorded.ID, "filename": recorded.Filename,
			"mime_type": recorded.MIMEType, "size_bytes": recorded.SizeBytes,
			"sha256": recorded.SHA256, "storage_path": recorded.StoragePath,
		})
	}
	return mustJSON(map[string]any{
		"generated": true, "profile": map[string]any{
			"id": profile.ID, "name": profile.Name, "revision": profile.Revision,
			"config_sha256": hex.EncodeToString(configHash[:]), "config": profile.ScreenshotConfig,
		},
		"tool": batch.Tool, "version": batch.Version, "input_path": inputPath,
		"media_duration_seconds": batch.DurationSeconds, "duration_ms": batch.DurationMS,
		"screenshot_count": len(artifactOutputs), "artifacts": artifactOutputs,
	}), nil
}

func screenshotInputs(snapshotBody json.RawMessage) (string, string, error) {
	type controls struct {
		Screenshots struct {
			Profile string `json:"profile"`
		} `json:"screenshots"`
	}
	var snapshot struct {
		JobInput      controls                   `json:"job_input"`
		ResumeState   controls                   `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return "", "", fmt.Errorf("decode screenshot step snapshot: %w", err)
	}
	profile := strings.TrimSpace(snapshot.JobInput.Screenshots.Profile)
	if resumed := strings.TrimSpace(snapshot.ResumeState.Screenshots.Profile); resumed != "" {
		profile = resumed
	}
	if profile == "" {
		profile = "default"
	}
	if !integrationNamePattern.MatchString(profile) {
		return "", "", fmt.Errorf("screenshot profile name is invalid")
	}
	var mediaInfo struct {
		SelectedPath string `json:"selected_path"`
	}
	body, exists := snapshot.PreviousSteps["media_info"]
	if !exists || json.Unmarshal(body, &mediaInfo) != nil || !strings.HasPrefix(mediaInfo.SelectedPath, "/") {
		return "", "", fmt.Errorf("media_info selected path evidence is missing or incomplete")
	}
	return profile, mediaInfo.SelectedPath, nil
}
