package worker

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/media"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeMediaInspector struct {
	selected string
	result   media.Inspection
	err      error
}

func (inspector *fakeMediaInspector) Inspect(_ context.Context, path string) (media.Inspection, error) {
	inspector.selected = path
	return inspector.result, inspector.err
}

func TestMediaInfoStepSelectsLargestCandidateAndStoresArtifact(t *testing.T) {
	directory := t.TempDir()
	small := filepath.Join(directory, "small.mkv")
	large := filepath.Join(directory, "large.mkv")
	if err := os.WriteFile(small, []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(large, []byte("larger"), 0o640); err != nil {
		t.Fatal(err)
	}
	inspector := &fakeMediaInspector{result: media.Inspection{
		Tool: "mediainfo", Version: "fixture", Format: "json", Document: []byte(`{"media":{"track":[]}}`), DurationMS: 5,
	}}
	recorder := &fakeArtifactRecorder{}
	executor := mediaInfoExecutor{mediaInspector: inspector, artifacts: mustArtifactStore(t), recorder: recorder}
	output, err := executor.Execute(context.Background(), mediaInfoExecution(directory, []string{small, large}))
	if err != nil {
		t.Fatal(err)
	}
	if inspector.selected != large || recorder.recorded.Kind != "mediainfo" || recorder.recorded.SHA256 == "" {
		t.Fatalf("selection/artifact = %s/%#v", inspector.selected, recorder.recorded)
	}
	var result struct {
		Kind         string `json:"kind"`
		SelectedPath string `json:"selected_path"`
	}
	if err := json.Unmarshal(output, &result); err != nil || result.Kind != "mediainfo" || result.SelectedPath != large {
		t.Fatalf("media output/error = %#v/%v", result, err)
	}
}

func TestMediaInfoStepUsesBDInfoForBlurayStructure(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "BDMV", "STREAM")
	if err := os.MkdirAll(directory, 0o750); err != nil {
		t.Fatal(err)
	}
	stream := filepath.Join(directory, "00001.m2ts")
	if err := os.WriteFile(stream, []byte("disc"), 0o640); err != nil {
		t.Fatal(err)
	}
	discInspector := &fakeMediaInspector{result: media.Inspection{Tool: "bdinfo", Version: "fixture", Format: "text", Document: []byte("DISC INFO:\nVideo: 1080p")}}
	recorder := &fakeArtifactRecorder{}
	executor := mediaInfoExecutor{mediaInspector: &fakeMediaInspector{}, discInspector: discInspector, artifacts: mustArtifactStore(t), recorder: recorder}
	output, err := executor.Execute(context.Background(), mediaInfoExecution(filepath.Dir(filepath.Dir(directory)), []string{stream}))
	if err != nil {
		t.Fatal(err)
	}
	var result struct {
		Kind         string `json:"kind"`
		SelectedPath string `json:"selected_path"`
	}
	if json.Unmarshal(output, &result) != nil || result.Kind != "bdinfo" || result.SelectedPath != filepath.Dir(filepath.Dir(directory)) || recorder.recorded.Kind != "bdinfo" {
		t.Fatalf("BDInfo output/artifact = %s/%#v", output, recorder.recorded)
	}
}

func TestMediaInfoStepBlocksDVDWithoutSubstitution(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "VIDEO_TS")
	if err := os.MkdirAll(directory, 0o750); err != nil {
		t.Fatal(err)
	}
	video := filepath.Join(directory, "VTS_01_1.VOB")
	if err := os.WriteFile(video, []byte("dvd"), 0o640); err != nil {
		t.Fatal(err)
	}
	executor := mediaInfoExecutor{mediaInspector: &fakeMediaInspector{}, artifacts: mustArtifactStore(t), recorder: &fakeArtifactRecorder{}}
	_, err := executor.Execute(context.Background(), mediaInfoExecution(filepath.Dir(directory), []string{video}))
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "dvdinfo_adapter_required" {
		t.Fatalf("DVD blocker = %#v", blocked)
	}
}

func mediaInfoExecution(root string, candidates []string) Execution {
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "media-step", InputSnapshot: mustJSON(map[string]any{
			"previous_steps": map[string]any{"content_resolve": map[string]any{
				"local_root": root, "media_candidates": candidates,
				"manifest_artifact_id": "manifest-id", "manifest_sha256": "manifest-sha",
			}},
		})},
		Attempt: workflow.Attempt{ID: "media-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "test-worker"},
	}
}
