package worker

import (
	"context"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeCandidateSource struct{ info sites.SourceInfo }

func (source fakeCandidateSource) ListCandidates(context.Context, string, sites.CandidateScanRequest) (sites.CandidateScanEvidence, error) {
	return sites.CandidateScanEvidence{}, nil
}

func (source fakeCandidateSource) Inspect(context.Context, sites.SourceReference) (sites.SourceInfo, error) {
	return source.info, nil
}

type fakeCandidateDuplicates struct{ evidence sites.TargetDuplicateEvidence }

func (provider fakeCandidateDuplicates) DuplicateCheck(context.Context, string, sites.TargetDuplicateQuery, workflow.Actor) (sites.TargetDuplicateEvidence, error) {
	return provider.evidence, nil
}

func TestCandidateEvaluationRequiresDownloadMetadataAndClearDuplicate(t *testing.T) {
	published := time.Date(2026, 8, 8, 1, 0, 0, 0, time.UTC)
	executor := candidateEvaluateExecutor{dependencies: candidateDependencies{
		source:     fakeCandidateSource{info: sites.SourceInfo{Tracker: "U2", TorrentID: "7", IMDbID: "tt1234567"}},
		duplicates: fakeCandidateDuplicates{evidence: sites.TargetDuplicateEvidence{SiteCode: "MTEAM", Duplicate: false}},
	}}
	policy := rules.Policy{
		Site: rules.Site{Code: "U2", Roles: []string{"source"}}, Source: rules.Source{Complete: true},
		Automation: rules.Automation{Download: true, Retorrent: true},
	}
	targetPolicy := policy
	targetPolicy.Site = rules.Site{Code: "MTEAM", Roles: []string{"target"}}
	targetPolicy.Automation = rules.Automation{Upload: true, Retorrent: true}
	result, err := executor.evaluateOne(context.Background(), dailyCandidateInput{Source: "U2", Target: "MTEAM"}, candidateRuleSnapshot{
		Source: candidateRuleSite{SiteCode: "U2", Fingerprint: "source", Policy: policy},
		Target: candidateRuleSite{SiteCode: "MTEAM", Fingerprint: "target", Policy: targetPolicy},
	}, sites.SourceCandidate{Tracker: "U2", TorrentID: "7", DetailsURL: "https://u2.dmhy.org/details.php?id=7", Title: "Fixture", Downloadable: true, Free: true, SizeBytes: 1024, PublishedAt: &published}, workflow.Actor{Type: "test"})
	if err != nil {
		t.Fatal(err)
	}
	if !result.Ready || len(result.Blockers) != 0 || result.DuplicateCheck == nil || result.Score <= 0 {
		t.Fatalf("candidate evaluation = %#v", result)
	}
}

func TestCandidateManualObligationsRemainVisibleRisks(t *testing.T) {
	result := candidateEvaluation{Source: sites.SourceCandidate{Title: "Fixture", Free: true}, Risks: []Blocker{}, Blockers: []Blocker{}, NextActions: []NextAction{}}
	applyCandidatePolicy(&result, candidateRuleSite{SiteCode: "U2", Fingerprint: "fingerprint", Policy: rules.Policy{
		Automation:  rules.Automation{Download: true, Retorrent: true, ManualReviewRequired: true},
		Transfer:    rules.Transfer{FreeleechRequired: true},
		Obligations: []rules.Obligation{{ID: "verify-transfer", Blocking: true, Verification: "manual", Resolution: "pending", Description: "Verify per-torrent transfer permission."}},
	}}, "source")
	result = finalizeCandidateEvaluation(result)
	if !result.Ready || len(result.Blockers) != 0 || len(result.Risks) != 2 || len(result.NextActions) != 1 {
		t.Fatalf("manual obligation result = %#v", result)
	}
}

func TestCandidateDateUsesShanghaiCalendarDay(t *testing.T) {
	now := time.Date(2026, 8, 7, 17, 0, 0, 0, time.UTC)
	if got := candidateDate("", now).Format("2006-01-02"); got != "2026-08-08" {
		t.Fatalf("candidate date = %s", got)
	}
}
