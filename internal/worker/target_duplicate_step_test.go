package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/sites/mteam"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeTargetDuplicateChecker struct {
	result mteam.DuplicateEvidence
	err    error
	calls  int
	target string
	query  mteam.DuplicateQuery
}

func (checker *fakeTargetDuplicateChecker) DuplicateCheck(_ context.Context, target string, query sites.TargetDuplicateQuery, _ workflow.Actor) (sites.TargetDuplicateEvidence, error) {
	checker.calls++
	checker.target = target
	checker.query = query
	return checker.result, checker.err
}

func TestTargetDuplicateStepPersistsCleanEvidence(t *testing.T) {
	store := mustArtifactStore(t)
	recorder := &fakeArtifactRecorder{}
	checker := &fakeTargetDuplicateChecker{result: mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("a", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt1234567"}, CheckedAt: time.Unix(1, 0).UTC(), Candidates: []mteam.DuplicateCandidate{},
	}}
	output, err := (targetDuplicateExecutor{checker: checker, artifacts: store, recorder: recorder}).Execute(
		context.Background(), targetDuplicateExecution(t, store, "https://www.imdb.com/title/tt1234567/", map[string]any{}),
	)
	if err != nil {
		t.Fatal(err)
	}
	if checker.calls != 1 || checker.target != "MTEAM" || checker.query.IMDbID != "tt1234567" || recorder.recorded.Kind != "duplicate_check" {
		t.Fatalf("checker/recorded = %#v/%#v", checker, recorder.recorded)
	}
	var result struct {
		Checked    bool   `json:"checked"`
		Status     string `json:"status"`
		Duplicate  bool   `json:"duplicate"`
		ArtifactID string `json:"duplicate_check_artifact_id"`
	}
	if err := json.Unmarshal(output, &result); err != nil || !result.Checked || result.Status != "clean" || result.Duplicate || result.ArtifactID != "artifact-id" {
		t.Fatalf("clean output/error = %#v/%v", result, err)
	}
}

func TestTargetDuplicateStepPersistsMatchThenBlocks(t *testing.T) {
	store := mustArtifactStore(t)
	recorder := &fakeArtifactRecorder{}
	checker := &fakeTargetDuplicateChecker{result: mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("a", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt1234567"}, Duplicate: true, ResultCount: 1,
		Candidates: []mteam.DuplicateCandidate{{ID: "42", Name: "Existing.Release", SizeBytes: 13}}, CheckedAt: time.Unix(1, 0).UTC(),
	}}
	_, err := (targetDuplicateExecutor{checker: checker, artifacts: store, recorder: recorder}).Execute(
		context.Background(), targetDuplicateExecution(t, store, "https://www.imdb.com/title/tt1234567/", map[string]any{}),
	)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_duplicate_detected" || blocked.NextActions[0].Action != "review_duplicate_candidates" ||
		recorder.recorded.Kind != "duplicate_check" {
		t.Fatalf("duplicate blocker/artifact = %#v/%#v", blocked, recorder.recorded)
	}
}

func TestTargetDuplicateStepRequiresIdentityButAcceptsResumeOverride(t *testing.T) {
	store := mustArtifactStore(t)
	checker := &fakeTargetDuplicateChecker{result: mteam.DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("a", 64),
		Query: mteam.DuplicateQuery{IMDbID: "tt7654321"}, CheckedAt: time.Unix(1, 0).UTC(),
	}}
	executor := targetDuplicateExecutor{checker: checker, artifacts: store, recorder: &fakeArtifactRecorder{}}
	_, err := executor.Execute(context.Background(), targetDuplicateExecution(t, store, "", map[string]any{}))
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_duplicate_identity_required" || checker.calls != 0 {
		t.Fatalf("identity blocker/checker = %#v/%#v", blocked, checker)
	}
	if _, err := executor.Execute(context.Background(), targetDuplicateExecution(t, store, "", map[string]any{"imdb_id": "tt7654321"})); err != nil {
		t.Fatalf("resumed duplicate check error = %v", err)
	}
	if checker.calls != 1 || checker.query.IMDbID != "tt7654321" {
		t.Fatalf("resumed checker = %#v", checker)
	}
}

func TestTargetDuplicateStepRejectsIdentityThatConflictsWithPackage(t *testing.T) {
	store := mustArtifactStore(t)
	checker := &fakeTargetDuplicateChecker{}
	executor := targetDuplicateExecutor{checker: checker, artifacts: store, recorder: &fakeArtifactRecorder{}}
	_, err := executor.Execute(context.Background(), targetDuplicateExecution(
		t, store, "https://www.imdb.com/title/tt1234567/", map[string]any{"imdb_id": "tt7654321"},
	))
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_duplicate_identity_conflict" || checker.calls != 0 {
		t.Fatalf("identity conflict/checker = %#v/%#v", blocked, checker)
	}
}

func targetDuplicateExecution(t *testing.T, store *artifacts.LocalStore, imdbURL string, resume map[string]any) Execution {
	t.Helper()
	links := map[string]string{}
	fields := map[string]any{"name": "Fixture.Release", "category": 405, "standard": 1}
	if imdbURL != "" {
		links["imdb"] = imdbURL
		fields["imdb"] = imdbURL
	}
	prepared := sites.PreparedTargetPackage{
		SchemaVersion: 1, Target: "MTEAM", Adapter: "mteam_api", MetadataLinks: links,
		FormFields: fields, Description: "fixture", MediaInfo: json.RawMessage(`{"media":{"track":[]}}`),
	}
	body, err := json.Marshal(prepared)
	if err != nil {
		t.Fatal(err)
	}
	file, err := store.Write(context.Background(), artifacts.Scope{
		JobID: "job-id", StepID: "package-step", AttemptID: "package-attempt",
	}, "mteam-target-package.json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "duplicate-step", InputSnapshot: mustJSON(map[string]any{
			"job_input":    map[string]any{"target_duplicate_check": map[string]any{}},
			"resume_state": map[string]any{"target_duplicate_check": resume},
			"previous_steps": map[string]any{"target_package": map[string]any{
				"prepared": true, "target": "MTEAM", "package_artifact_id": "package-id",
				"package_sha256": file.SHA256, "package_storage_path": file.RelativePath, "package_size_bytes": file.SizeBytes,
			}},
		})},
		Attempt: workflow.Attempt{ID: "duplicate-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "test-worker"},
	}
}
