package worker

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestMetadataStepBuildsAuditedIdentityArtifact(t *testing.T) {
	artifactStore := mustArtifactStore(t)
	recorder := &fakeArtifactRecorder{}
	executor := metadataExecutor{artifacts: artifactStore, recorder: recorder}
	execution := metadataExecution(map[string]any{}, sites.SourceInfo{
		Name: "Fixture Film", IMDbID: "tt1234567", TMDbID: "7654", TMDbType: "movie", DoubanID: "2345678",
	})
	output, err := executor.Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if recorder.recorded.Kind != "metadata" || recorder.recorded.SHA256 == "" {
		t.Fatalf("metadata artifact = %#v", recorder.recorded)
	}
	var result struct {
		Identity         metadataIdentity  `json:"identity"`
		Links            map[string]string `json:"links"`
		IdentityStrength string            `json:"identity_strength"`
		ManualReview     bool              `json:"manual_review_required"`
	}
	if err := json.Unmarshal(output, &result); err != nil || result.Identity.Title != "Fixture Film" ||
		result.Links["imdb"] == "" || result.Links["tmdb"] == "" || result.IdentityStrength != "external_id" || result.ManualReview {
		t.Fatalf("metadata output/error = %#v/%v", result, err)
	}
	file, err := artifactStore.Open(recorder.recorded.StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	var document metadataDocument
	if err := json.NewDecoder(file).Decode(&document); err != nil || document.Content["manifest_sha256"] != "manifest-sha" {
		t.Fatalf("metadata document/error = %#v/%v", document, err)
	}
}

func TestMetadataStepUsesResumeOverrideAndBlocksInvalidIdentity(t *testing.T) {
	executor := metadataExecutor{artifacts: mustArtifactStore(t), recorder: &fakeArtifactRecorder{}}
	execution := metadataExecution(map[string]any{"metadata": map[string]any{
		"title": "Manual Anime", "anidb_id": "3456",
	}}, sites.SourceInfo{})
	output, err := executor.Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	var result struct {
		Identity metadataIdentity `json:"identity"`
	}
	if err := json.Unmarshal(output, &result); err != nil || result.Identity.Title != "Manual Anime" || result.Identity.AniDBID != "3456" {
		t.Fatalf("override output/error = %#v/%v", result, err)
	}

	execution = metadataExecution(map[string]any{"metadata": map[string]any{"title": "Bad", "imdb_id": "123"}}, sites.SourceInfo{})
	_, err = executor.Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "metadata_identity_required" {
		t.Fatalf("invalid metadata blocker = %#v", blocked)
	}
}

func metadataExecution(resumeState map[string]any, info sites.SourceInfo) Execution {
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "metadata-step", InputSnapshot: mustJSON(map[string]any{
			"resume_state": resumeState,
			"previous_steps": map[string]any{
				"source_inspect": map[string]any{
					"source_info":          info,
					"description_artifact": map[string]any{"artifact_id": "description-id", "sha256": "description-sha"},
				},
				"content_resolve": map[string]any{
					"resolved": true, "local_root": "/downloads/release", "file_count": 1,
					"total_size_bytes": 13, "manifest_artifact_id": "manifest-id",
					"manifest_sha256": "manifest-sha", "media_candidates": []string{"/downloads/release/video.mkv"},
				},
			},
		})},
		Attempt: workflow.Attempt{ID: "metadata-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "test-worker"},
	}
}
