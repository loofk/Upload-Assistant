package worker

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/metadataproviders"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeMetadataResolver struct {
	result    metadataproviders.ResolveResult
	err       error
	name      string
	request   metadataproviders.ResolveRequest
	callCount int
}

func (resolver *fakeMetadataResolver) Resolve(_ context.Context, name string, request metadataproviders.ResolveRequest, _ workflow.Actor) (metadataproviders.ResolveResult, error) {
	resolver.name, resolver.request = name, request
	resolver.callCount++
	return resolver.result, resolver.err
}

func TestMetadataTMDbStepPersistsProviderEvidence(t *testing.T) {
	store := mustArtifactStore(t)
	recorder := &fakeArtifactRecorder{}
	resolver := &fakeMetadataResolver{result: metadataproviders.ResolveResult{
		Name: "tmdb-main", Adapter: "tmdb", Matched: true,
		Identity:            metadataproviders.Identity{IMDbID: "tt1234567", TMDbID: "42", TMDbType: "movie"},
		ConfigurationSHA256: strings.Repeat("a", 64), QuerySHA256: strings.Repeat("b", 64),
		Calls: []metadataproviders.CallEvidence{{Sequence: 1, Purpose: "external_ids", QuerySHA256: strings.Repeat("c", 64), ResponseSHA256: strings.Repeat("d", 64), StatusCode: 200}},
	}}
	executor := metadataTMDbExecutor{resolver: resolver, artifacts: store, recorder: recorder}
	output, err := executor.Execute(context.Background(), metadataProviderExecution("metadata_tmdb", map[string]any{
		"metadata_providers": map[string]any{"tmdb": "tmdb-main"},
	}, map[string]any{}, map[string]any{
		"metadata": map[string]any{"identity": metadataIdentity{Title: "Fixture", IMDbID: "tt1234567", TMDbID: "42", TMDbType: "movie"}},
	}))
	if err != nil {
		t.Fatal(err)
	}
	var result struct {
		Identity       metadataIdentity `json:"identity"`
		Provider       string           `json:"provider"`
		ArtifactSHA256 string           `json:"artifact_sha256"`
	}
	if json.Unmarshal(output, &result) != nil || result.Provider != "tmdb-main" || result.Identity.TMDbID != "42" || len(result.ArtifactSHA256) != 64 || recorder.recorded.Kind != "metadata_tmdb" {
		t.Fatalf("output/artifact = %s / %#v", output, recorder.recorded)
	}
	if resolver.callCount != 1 || resolver.request.IMDbID != "tt1234567" || resolver.request.TMDbID != "42" {
		t.Fatalf("resolver call = %d %#v", resolver.callCount, resolver.request)
	}
}

func TestMetadataTMDbStepBlocksIncompleteIdentityWithoutProvider(t *testing.T) {
	executor := metadataTMDbExecutor{artifacts: mustArtifactStore(t), recorder: &fakeArtifactRecorder{}}
	_, err := executor.Execute(context.Background(), metadataProviderExecution("metadata_tmdb", map[string]any{}, map[string]any{}, map[string]any{
		"metadata": map[string]any{"identity": metadataIdentity{Title: "Fixture", IMDbID: "tt1234567"}},
	}))
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "metadata_tmdb_required" || len(blocked.NextActions) != 1 {
		t.Fatalf("blocker = %#v", blocked)
	}
}

func TestMetadataPTGenStepStoresRawTextOnlyInArtifact(t *testing.T) {
	store := mustArtifactStore(t)
	recorder := &fakeArtifactRecorder{}
	const description = "[img]https://img.example/poster.jpg[/img]\n[b]Fixture Douban description[/b]"
	resolver := &fakeMetadataResolver{result: metadataproviders.ResolveResult{
		Name: "ptgen-main", Adapter: "ptgen", Matched: true,
		Identity:    metadataproviders.Identity{IMDbID: "tt1234567", TMDbID: "42", TMDbType: "movie", DoubanID: "1292052"},
		Description: description, DescriptionSHA256: sha256Hex([]byte(description)),
		ConfigurationSHA256: strings.Repeat("e", 64), QuerySHA256: strings.Repeat("f", 64),
		Calls: []metadataproviders.CallEvidence{{Sequence: 1, Purpose: "douban_description", QuerySHA256: strings.Repeat("1", 64), ResponseSHA256: strings.Repeat("2", 64), StatusCode: 200}},
	}}
	executor := metadataPTGenExecutor{resolver: resolver, artifacts: store, recorder: recorder}
	output, err := executor.Execute(context.Background(), metadataProviderExecution("metadata_ptgen", map[string]any{
		"metadata_providers": map[string]any{"ptgen": "ptgen-main"},
	}, map[string]any{}, map[string]any{
		"metadata_tmdb": map[string]any{"identity": metadataIdentity{Title: "Fixture", IMDbID: "tt1234567", TMDbID: "42", TMDbType: "movie"}},
	}))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(output), "Fixture Douban description") || recorder.recorded.Kind != "metadata_ptgen" {
		t.Fatalf("step output leaked raw description or wrong artifact: %s / %#v", output, recorder.recorded)
	}
	file, err := store.Open(recorder.recorded.StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	var document metadataPTGenDocument
	if json.NewDecoder(file).Decode(&document) != nil || document.Description != description || document.Identity.DoubanID != "1292052" {
		t.Fatalf("artifact document = %#v", document)
	}
}

func TestMetadataPTGenStepAcceptsAuditedManualRecovery(t *testing.T) {
	store := mustArtifactStore(t)
	recorder := &fakeArtifactRecorder{}
	executor := metadataPTGenExecutor{artifacts: store, recorder: recorder}
	output, err := executor.Execute(context.Background(), metadataProviderExecution("metadata_ptgen", map[string]any{}, map[string]any{
		"metadata_ptgen": map[string]any{"douban_id": "1292052", "description": "人工复核的豆瓣简介"},
	}, map[string]any{
		"metadata_tmdb": map[string]any{"identity": metadataIdentity{Title: "Fixture", IMDbID: "tt1234567", TMDbID: "42", TMDbType: "movie"}},
	}))
	if err != nil || !strings.Contains(string(output), `"source":"manual"`) || recorder.recorded.Kind != "metadata_ptgen" {
		t.Fatalf("manual output/error/artifact = %s/%v/%#v", output, err, recorder.recorded)
	}
}

func metadataProviderExecution(step string, jobInput, resumeState, previous map[string]any) Execution {
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: step + "-step", InputSnapshot: mustJSON(map[string]any{
			"job_input": jobInput, "resume_state": resumeState, "previous_steps": previous,
		})}, Attempt: workflow.Attempt{ID: step + "-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "fixture"},
	}
}
