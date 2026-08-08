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
		Tool: "mediainfo", Version: "fixture", Document: json.RawMessage(`{"media":{"track":[]}}`), DurationMS: 5,
	}}
	recorder := &fakeArtifactRecorder{}
	executor := mediaInfoExecutor{inspector: inspector, artifacts: mustArtifactStore(t), recorder: recorder}
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

func TestMediaInfoStepRequiresBDInfoForDiscStructure(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "BDMV", "STREAM")
	if err := os.MkdirAll(directory, 0o750); err != nil {
		t.Fatal(err)
	}
	stream := filepath.Join(directory, "00001.m2ts")
	if err := os.WriteFile(stream, []byte("disc"), 0o640); err != nil {
		t.Fatal(err)
	}
	executor := mediaInfoExecutor{inspector: &fakeMediaInspector{}, artifacts: mustArtifactStore(t), recorder: &fakeArtifactRecorder{}}
	_, err := executor.Execute(context.Background(), mediaInfoExecution(filepath.Dir(filepath.Dir(directory)), []string{stream}))
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "bdinfo_adapter_required" {
		t.Fatalf("disc blocker = %#v", blocked)
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
