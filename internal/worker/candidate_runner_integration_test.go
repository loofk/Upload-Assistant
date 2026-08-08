package worker

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/candidates"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type candidateRuleMap map[string]rules.Revision

func (provider candidateRuleMap) Active(_ context.Context, siteCode string) (rules.Revision, error) {
	return provider[siteCode], nil
}

type candidateFixtureSource struct{}

func (candidateFixtureSource) ListCandidates(_ context.Context, siteCode string, request sites.CandidateScanRequest) (sites.CandidateScanEvidence, error) {
	published := time.Date(2026, 8, 8, 1, 0, 0, 0, time.UTC)
	return sites.CandidateScanEvidence{SiteCode: siteCode, Page: request.Page, Limit: request.Limit, ScannedAt: published, Items: []sites.SourceCandidate{{
		Tracker: siteCode, TorrentID: "60635", DetailsURL: "https://u2.dmhy.org/details.php?id=60635",
		Title: "Fixture Anime 2026 1080p", SizeBytes: 5 << 30, PublishedAt: &published,
		PromotionLabels: []string{"free"}, Free: true, Downloadable: true, DownloadBlockers: []string{},
	}}}, nil
}

func (candidateFixtureSource) Inspect(_ context.Context, reference sites.SourceReference) (sites.SourceInfo, error) {
	return sites.SourceInfo{
		Tracker: reference.Tracker, TorrentID: reference.TorrentID,
		DetailsURL: "https://u2.dmhy.org/details.php?id=" + reference.TorrentID,
		Name:       "Fixture Anime 2026 1080p", IMDbID: "tt1234567", TMDbID: "9876", TMDbType: "tv",
		DoubanID: "2345678", PromotionLabels: []string{"free"}, Free: true, RetrievedAt: time.Now().UTC(),
	}, nil
}

type candidateFixtureDuplicates struct{}

func (candidateFixtureDuplicates) DuplicateCheck(_ context.Context, siteCode string, query sites.TargetDuplicateQuery, _ workflow.Actor) (sites.TargetDuplicateEvidence, error) {
	return sites.TargetDuplicateEvidence{SiteCode: siteCode, Adapter: "fixture", Query: query, Duplicate: false, Candidates: []sites.TargetDuplicateCandidate{}, CheckedAt: time.Now().UTC()}, nil
}

func TestDailyCandidateRunnerCompletesAuditedFixture(t *testing.T) {
	databaseURL := os.Getenv("UA_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("UA_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}

	workflowStore := workflow.NewStore(pool)
	definition := workflow.DailyCandidatesDefinition()
	workflowID, err := workflowStore.EnsureDefinition(ctx, definition)
	if err != nil {
		t.Fatal(err)
	}
	runtime := workflow.NewService(workflowStore, definition, workflowID)
	candidateStore := candidates.NewStore(pool)
	artifactStore, err := artifacts.NewLocalStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	policyFor := func(code, role string, automation rules.Automation) rules.Revision {
		body, _ := json.Marshal(rules.Policy{
			SchemaVersion: 1, Site: rules.Site{Code: code, DisplayName: code, Roles: []string{role}},
			Source:     rules.Source{URL: "https://example.invalid/rules", CapturedAt: "2026-08-08", Complete: true, Scope: "fixture"},
			Automation: automation, Obligations: []rules.Obligation{},
		})
		return rules.Revision{ID: uuid.NewString(), SiteCode: code, Status: "approved", Fingerprint: code + "-fixture-fingerprint", Policy: body}
	}
	rulesProvider := candidateRuleMap{
		"U2":    policyFor("U2", "source", rules.Automation{Download: true, Retorrent: true}),
		"MTEAM": policyFor("MTEAM", "target", rules.Automation{Upload: true, Retorrent: true}),
	}
	runner := New(runtime, "candidate-fixture-worker", slog.New(slog.NewTextHandler(io.Discard, nil)),
		WithDailyCandidates(rulesProvider, candidateFixtureSource{}, candidateFixtureDuplicates{}, candidateStore, artifactStore),
	)
	job, err := runtime.CreateJob(ctx, workflow.CreateJobInput{
		Kind: "daily_candidates", ExecutionMode: workflow.ExecutionAuto,
		Input:          mustJSON(map[string]any{"source": "U2", "target": "MTEAM", "target_count": 1, "scan_limit": 1, "date": "2099-08-08"}),
		IdempotencyKey: "candidate-fixture-" + uuid.NewString(), Owner: "candidate-runner-integration",
		Actor: workflow.Actor{Type: "test", ID: "candidate-runner-integration"},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), "DELETE FROM candidate_items WHERE discovery_job_id = $1", job.ID)
		_, _ = pool.Exec(context.Background(), "DELETE FROM jobs WHERE id = $1", job.ID)
	})
	for range definition.Steps {
		if err := runner.RunOnce(ctx); err != nil {
			t.Fatalf("RunOnce() error = %v", err)
		}
	}
	completed, err := runtime.GetJob(ctx, job.ID)
	if err != nil || completed.Status != workflow.JobComplete {
		t.Fatalf("completed job/error = %#v/%v", completed, err)
	}
	var summary struct {
		OK            bool                         `json:"ok"`
		Status        string                       `json:"status"`
		SelectedCount int                          `json:"selected_count"`
		SummaryFile   sites.TargetArtifactEvidence `json:"summary_file"`
	}
	if err := json.Unmarshal(completed.Summary, &summary); err != nil || !summary.OK || summary.Status != "complete" || summary.SelectedCount != 1 || summary.SummaryFile.SHA256 == "" {
		t.Fatalf("candidate summary/error = %#v/%v", summary, err)
	}
	date := time.Date(2099, 8, 8, 0, 0, 0, 0, time.UTC)
	items, err := candidateStore.List(ctx, candidates.ListFilter{SourceSite: "U2", TargetSite: "MTEAM", RecommendationDate: &date, Status: candidates.StatusCandidate, Limit: 10})
	if err != nil || len(items) != 1 || items[0].Rank == nil || *items[0].Rank != 1 {
		t.Fatalf("persisted candidates/error = %#v/%v", items, err)
	}
	events, err := runtime.ListEvents(ctx, job.ID, 0, 100)
	if err != nil || len(events) < len(definition.Steps)*2 {
		t.Fatalf("audit events/error = %d/%v", len(events), err)
	}
}
