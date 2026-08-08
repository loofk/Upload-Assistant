package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/media"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeScreenshotProfiles struct {
	profile integrations.RuntimeScreenshotProfile
	err     error
}

func (provider fakeScreenshotProfiles) GetRuntimeScreenshotProfile(context.Context, string) (integrations.RuntimeScreenshotProfile, error) {
	return provider.profile, provider.err
}

type fakeScreenshotGenerator struct {
	input  string
	config integrations.ScreenshotConfig
	batch  media.ScreenshotBatch
	err    error
}

func (generator *fakeScreenshotGenerator) Generate(_ context.Context, input string, config integrations.ScreenshotConfig) (media.ScreenshotBatch, error) {
	generator.input, generator.config = input, config
	return generator.batch, generator.err
}

type collectingArtifactRecorder struct {
	inputs []workflow.RegisterArtifactInput
}

func (recorder *collectingArtifactRecorder) RegisterArtifact(_ context.Context, input workflow.RegisterArtifactInput) (workflow.Artifact, error) {
	recorder.inputs = append(recorder.inputs, input)
	return workflow.Artifact{
		ID: fmt.Sprintf("artifact-%d", len(recorder.inputs)), JobID: input.JobID,
		StepID: input.StepID, AttemptID: input.AttemptID, Kind: input.Kind,
		StorageBackend: "local", StoragePath: input.StoragePath, Filename: input.Filename,
		MIMEType: input.MIMEType, SizeBytes: input.SizeBytes, SHA256: input.SHA256,
	}, nil
}

func TestScreenshotsStepUsesImmutableProfileAndRegistersEachImage(t *testing.T) {
	profileConfig := integrations.ScreenshotConfig{Count: 2, Format: "png", Quality: 90, StartPercent: 0.1, EndPercent: 0.9}
	profileBody := mustJSON(profileConfig)
	profiles := fakeScreenshotProfiles{profile: integrations.RuntimeScreenshotProfile{
		ScreenshotProfile: integrations.ScreenshotProfile{ID: "profile-id", Name: "default", Revision: 3, Enabled: true, Config: profileBody},
		ScreenshotConfig:  profileConfig,
	}}
	generator := &fakeScreenshotGenerator{batch: media.ScreenshotBatch{
		Tool: "ffmpeg", Version: "fixture", DurationSeconds: 100, DurationMS: 10,
		Screenshots: []media.Screenshot{
			{Index: 1, Timestamp: 30, Format: "png", Filename: "screenshot-01.png", MIMEType: "image/png", Bytes: []byte("\x89PNG\r\n\x1a\nfirst"), SizeBytes: 13},
			{Index: 2, Timestamp: 70, Format: "png", Filename: "screenshot-02.png", MIMEType: "image/png", Bytes: []byte("\x89PNG\r\n\x1a\nsecond"), SizeBytes: 14},
		},
	}}
	recorder := &collectingArtifactRecorder{}
	executor := screenshotsExecutor{profiles: profiles, generator: generator, artifacts: mustArtifactStore(t), recorder: recorder}
	output, err := executor.Execute(context.Background(), screenshotExecution("default"))
	if err != nil {
		t.Fatal(err)
	}
	if len(recorder.inputs) != 2 || recorder.inputs[0].Kind != "screenshot" || generator.config.Count != 2 {
		t.Fatalf("screenshot records/config = %#v/%#v", recorder.inputs, generator.config)
	}
	var result struct {
		Generated       bool `json:"generated"`
		ScreenshotCount int  `json:"screenshot_count"`
	}
	if err := json.Unmarshal(output, &result); err != nil || !result.Generated || result.ScreenshotCount != 2 {
		t.Fatalf("screenshots output/error = %#v/%v", result, err)
	}
}

func TestScreenshotsStepBlocksMissingProfile(t *testing.T) {
	executor := screenshotsExecutor{
		profiles:  fakeScreenshotProfiles{err: integrations.ErrNotFound},
		generator: &fakeScreenshotGenerator{}, artifacts: mustArtifactStore(t), recorder: &collectingArtifactRecorder{},
	}
	_, err := executor.Execute(context.Background(), screenshotExecution("missing"))
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "screenshot_profile_required" {
		t.Fatalf("missing profile blocker = %#v", blocked)
	}
}

func screenshotExecution(profile string) Execution {
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "screenshots-step", InputSnapshot: mustJSON(map[string]any{
			"job_input":      map[string]any{"screenshots": map[string]any{"profile": profile}},
			"previous_steps": map[string]any{"media_info": map[string]any{"selected_path": "/downloads/release/video.mkv"}},
		})},
		Attempt: workflow.Attempt{ID: "screenshots-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "test-worker"},
	}
}
