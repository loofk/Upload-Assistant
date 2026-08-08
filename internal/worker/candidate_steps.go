package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/candidates"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const maxCandidateArtifactBytes = 16 << 20

type CandidateSourceProvider interface {
	ListCandidates(context.Context, string, sites.CandidateScanRequest) (sites.CandidateScanEvidence, error)
	Inspect(context.Context, sites.SourceReference) (sites.SourceInfo, error)
}

type CandidateDuplicateProvider interface {
	DuplicateCheck(context.Context, string, sites.TargetDuplicateQuery, workflow.Actor) (sites.TargetDuplicateEvidence, error)
}

type CandidateRepository interface {
	Upsert(context.Context, candidates.UpsertInput) (candidates.Item, error)
}

func WithDailyCandidates(
	ruleProvider RuleProvider,
	sourceProvider CandidateSourceProvider,
	duplicateProvider CandidateDuplicateProvider,
	repository CandidateRepository,
	artifactStore WorkflowArtifactStore,
) Option {
	return func(runner *Runner) {
		dependencies := candidateDependencies{
			rules: ruleProvider, source: sourceProvider, duplicates: duplicateProvider,
			repository: repository, artifacts: artifactStore, recorder: runner.runtime,
			now: time.Now,
		}
		runner.executors["candidate_rules"] = candidateRulesExecutor{dependencies: dependencies}
		runner.executors["candidate_scan"] = candidateScanExecutor{dependencies: dependencies}
		runner.executors["candidate_evaluate"] = candidateEvaluateExecutor{dependencies: dependencies}
		runner.executors["candidate_rank"] = candidateRankExecutor{dependencies: dependencies}
		runner.executors["candidate_summary"] = candidateSummaryExecutor{dependencies: dependencies}
	}
}

type candidateDependencies struct {
	rules      RuleProvider
	source     CandidateSourceProvider
	duplicates CandidateDuplicateProvider
	repository CandidateRepository
	artifacts  WorkflowArtifactStore
	recorder   ArtifactRecorder
	now        func() time.Time
}

type dailyCandidateInput struct {
	Source      string `json:"source"`
	Target      string `json:"target"`
	TargetCount int    `json:"target_count"`
	ScanLimit   int    `json:"scan_limit"`
	Page        int    `json:"page"`
	Date        string `json:"date,omitempty"`
}

func candidateInput(body json.RawMessage) (dailyCandidateInput, error) {
	var input dailyCandidateInput
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		return input, fmt.Errorf("decode daily candidate input: %w", err)
	}
	input.Source = strings.ToUpper(strings.TrimSpace(input.Source))
	input.Target = strings.ToUpper(strings.TrimSpace(input.Target))
	if input.Source == "" || input.Target == "" || input.Source == input.Target {
		return input, errors.New("different source and target site codes are required")
	}
	if input.TargetCount == 0 {
		input.TargetCount = 10
	}
	if input.TargetCount < 1 || input.TargetCount > 25 {
		return input, errors.New("target_count must be between 1 and 25")
	}
	if input.ScanLimit == 0 {
		input.ScanLimit = max(20, input.TargetCount*3)
	}
	if input.ScanLimit < input.TargetCount || input.ScanLimit > 100 {
		return input, errors.New("scan_limit must be between target_count and 100")
	}
	if input.Page == 0 {
		input.Page = 1
	}
	if input.Page < 1 || input.Page > 1000 {
		return input, errors.New("page must be between 1 and 1000")
	}
	if input.Date != "" {
		if _, err := time.Parse("2006-01-02", input.Date); err != nil {
			return input, errors.New("date must use YYYY-MM-DD")
		}
	}
	return input, nil
}

type candidateRuleSnapshot struct {
	Source candidateRuleSite `json:"source"`
	Target candidateRuleSite `json:"target"`
}

type candidateRuleSite struct {
	SiteCode    string       `json:"site_code"`
	RevisionID  string       `json:"revision_id"`
	Fingerprint string       `json:"fingerprint"`
	Policy      rules.Policy `json:"policy"`
}

type candidateRulesExecutor struct{ dependencies candidateDependencies }

func (executor candidateRulesExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.dependencies.rules == nil {
		return nil, fmt.Errorf("candidate rule provider is unavailable")
	}
	input, err := candidateInput(execution.Job.Input)
	if err != nil {
		return nil, candidateInputBlock(err)
	}
	result := candidateRuleSnapshot{}
	for _, site := range []struct {
		role string
		code string
	}{
		{role: "source", code: input.Source},
		{role: "target", code: input.Target},
	} {
		role, siteCode := site.role, site.code
		revision, err := executor.dependencies.rules.Active(ctx, siteCode)
		if errors.Is(err, rules.ErrNotFound) || (err == nil && revision.Status != "approved") {
			return nil, activeRuleRequired(siteCode)
		}
		if err != nil {
			return nil, fmt.Errorf("load active candidate rules for %s: %w", siteCode, err)
		}
		policy, err := rules.ParsePolicy(revision.Policy)
		if err != nil {
			return nil, fmt.Errorf("parse active candidate rules for %s: %w", siteCode, err)
		}
		if !policy.Source.Complete {
			return nil, &BlockError{
				Blockers:    []Blocker{{Code: "rule_source_incomplete", SiteCode: siteCode, Message: "candidate scanning requires a complete active rule source"}},
				NextActions: []NextAction{{Action: "import_complete_rule_revision", Parameters: map[string]any{"site_code": siteCode}}},
				ResumeState: map[string]any{"site_code": siteCode},
			}
		}
		if !slices.Contains(policy.Site.Roles, role) {
			return nil, &BlockError{
				Blockers:    []Blocker{{Code: "rule_role_not_allowed", SiteCode: siteCode, Message: "active rules do not cover the required candidate workflow role"}},
				NextActions: []NextAction{{Action: "review_active_rules", Parameters: map[string]any{"site_code": siteCode, "role": role}}},
				ResumeState: map[string]any{"site_code": siteCode, "role": role},
			}
		}
		snapshot := candidateRuleSite{SiteCode: siteCode, RevisionID: revision.ID, Fingerprint: revision.Fingerprint, Policy: policy}
		if role == "source" {
			result.Source = snapshot
		} else {
			result.Target = snapshot
		}
	}
	return mustJSON(map[string]any{"rules": result}), nil
}

type candidateScanExecutor struct{ dependencies candidateDependencies }

func (executor candidateScanExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.dependencies.source == nil || !candidateArtifactsReady(executor.dependencies) {
		return nil, fmt.Errorf("candidate scan dependencies are unavailable")
	}
	input, err := candidateInput(execution.Job.Input)
	if err != nil {
		return nil, candidateInputBlock(err)
	}
	evidence, err := executor.dependencies.source.ListCandidates(ctx, input.Source, sites.CandidateScanRequest{Limit: input.ScanLimit, Page: input.Page})
	if err != nil {
		return nil, candidateProviderBlock(err, input.Source, "list_source_candidates")
	}
	artifact, err := persistCandidateArtifact(ctx, execution, executor.dependencies, "candidate_scan", "candidate-scan.json", evidence)
	if err != nil {
		return nil, err
	}
	return mustJSON(map[string]any{
		"source": input.Source, "target": input.Target, "scanned_count": len(evidence.Items),
		"scan_limit": input.ScanLimit, "page": input.Page, "scan_artifact": artifact,
	}), nil
}

type candidateEvaluation struct {
	Source                sites.SourceCandidate          `json:"source"`
	Metadata              sites.SourceInfo               `json:"metadata"`
	DuplicateCheck        *sites.TargetDuplicateEvidence `json:"duplicate_check,omitempty"`
	Ready                 bool                           `json:"ready"`
	Score                 float64                        `json:"score"`
	RecommendationReasons []string                       `json:"recommendation_reasons"`
	Risks                 []Blocker                      `json:"risks"`
	Blockers              []Blocker                      `json:"blockers"`
	NextActions           []NextAction                   `json:"next_actions"`
}

type candidateEvaluationBatch struct {
	Source      string                `json:"source"`
	Target      string                `json:"target"`
	Rules       candidateRuleSnapshot `json:"rules"`
	Evaluations []candidateEvaluation `json:"evaluations"`
	EvaluatedAt time.Time             `json:"evaluated_at"`
}

type candidateEvaluateExecutor struct{ dependencies candidateDependencies }

func (executor candidateEvaluateExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.dependencies.source == nil || executor.dependencies.duplicates == nil || !candidateArtifactsReady(executor.dependencies) {
		return nil, fmt.Errorf("candidate evaluation dependencies are unavailable")
	}
	input, err := candidateInput(execution.Job.Input)
	if err != nil {
		return nil, candidateInputBlock(err)
	}
	rulesSnapshot, scan, err := loadCandidateInputs(ctx, execution, executor.dependencies.artifacts)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	batch := candidateEvaluationBatch{Source: input.Source, Target: input.Target, Rules: rulesSnapshot, Evaluations: make([]candidateEvaluation, 0, len(scan.Items)), EvaluatedAt: candidateNow(executor.dependencies)}
	for _, listed := range scan.Items {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		batch.Evaluations = append(batch.Evaluations, executor.evaluateOne(ctx, input, rulesSnapshot, listed, execution.Actor))
	}
	artifact, err := persistCandidateArtifact(ctx, execution, executor.dependencies, "candidate_evaluation", "candidate-evaluation.json", batch)
	if err != nil {
		return nil, err
	}
	readyCount := 0
	for _, evaluation := range batch.Evaluations {
		if evaluation.Ready {
			readyCount++
		}
	}
	return mustJSON(map[string]any{
		"evaluated_count": len(batch.Evaluations), "ready_count": readyCount,
		"blocked_count": len(batch.Evaluations) - readyCount, "evaluation_artifact": artifact,
	}), nil
}

func (executor candidateEvaluateExecutor) evaluateOne(
	ctx context.Context,
	input dailyCandidateInput,
	ruleSnapshot candidateRuleSnapshot,
	listed sites.SourceCandidate,
	actor workflow.Actor,
) candidateEvaluation {
	result := candidateEvaluation{Source: listed, RecommendationReasons: []string{}, Risks: []Blocker{}, Blockers: []Blocker{}, NextActions: []NextAction{}}
	if strings.TrimSpace(listed.DetailsURL) == "" {
		result.Blockers = append(result.Blockers, Blocker{Code: "candidate_details_url_unavailable", SiteCode: input.Source, Message: "source candidate details URL is unavailable"})
	}
	if listed.SizeBytes <= 0 {
		result.Blockers = append(result.Blockers, Blocker{Code: "candidate_size_unavailable", SiteCode: input.Source, Message: "source candidate size is unavailable"})
	}
	if listed.PublishedAt == nil {
		result.Blockers = append(result.Blockers, Blocker{Code: "candidate_published_at_unavailable", SiteCode: input.Source, Message: "source candidate publish time is unavailable"})
	}
	if listed.Downloadable {
		result.Score += 15
		result.RecommendationReasons = append(result.RecommendationReasons, "source_credentials_ready")
	} else {
		result.Blockers = append(result.Blockers, Blocker{Code: "source_not_downloadable", SiteCode: input.Source, Message: "source passkey/downloadability is not ready"})
		result.NextActions = append(result.NextActions, NextAction{Action: "configure_site_credentials", Parameters: map[string]any{"site_code": input.Source}})
	}
	if listed.Free {
		result.Score += 25
		result.RecommendationReasons = append(result.RecommendationReasons, "freeleech")
	} else if len(listed.PromotionLabels) > 0 {
		result.Score += 10
		result.RecommendationReasons = append(result.RecommendationReasons, "promoted")
	}
	if listed.SizeBytes > 0 {
		result.Score += 5
	}
	applyCandidatePolicy(&result, ruleSnapshot.Source, "source")
	applyCandidatePolicy(&result, ruleSnapshot.Target, "target")

	metadata, err := executor.dependencies.source.Inspect(ctx, sites.SourceReference{Tracker: input.Source, TorrentID: listed.TorrentID})
	if err != nil {
		code, message, _ := sites.ErrorDetails(err)
		result.Blockers = append(result.Blockers, Blocker{Code: code, SiteCode: input.Source, Message: message})
		result.NextActions = append(result.NextActions, NextAction{Action: "inspect_source_candidate", Parameters: map[string]any{"source_url": listed.DetailsURL}})
		result.Score -= 30
		return finalizeCandidateEvaluation(result)
	}
	result.Metadata = metadata
	if strings.TrimSpace(result.Source.Title) == "" {
		result.Source.Title = strings.TrimSpace(metadata.Name)
	}
	if result.Source.Title == "" {
		result.Blockers = append(result.Blockers, Blocker{Code: "candidate_title_unavailable", SiteCode: input.Source, Message: "source candidate title is unavailable"})
	}
	if metadata.IMDbID != "" || metadata.TMDbID != "" || metadata.DoubanID != "" || metadata.AniDBID != "" {
		result.Score += 10
		result.RecommendationReasons = append(result.RecommendationReasons, "metadata_available")
	} else {
		result.Blockers = append(result.Blockers, Blocker{Code: "candidate_metadata_unavailable", SiteCode: input.Source, Message: "IMDb/TMDb/豆瓣/AniDB metadata is unavailable"})
	}
	if metadata.IMDbID == "" {
		result.Blockers = append(result.Blockers, Blocker{Code: "target_duplicate_identity_required", SiteCode: input.Target, Message: "target duplicate check requires an IMDb id"})
		result.NextActions = append(result.NextActions, NextAction{Action: "resolve_candidate_metadata", Parameters: map[string]any{"source_url": listed.DetailsURL}})
		return finalizeCandidateEvaluation(result)
	}
	duplicate, err := executor.dependencies.duplicates.DuplicateCheck(ctx, input.Target, sites.TargetDuplicateQuery{IMDbID: metadata.IMDbID}, actor)
	if err != nil {
		code, message, _ := sites.ErrorDetails(err)
		result.Blockers = append(result.Blockers, Blocker{Code: code, SiteCode: input.Target, Message: message})
		result.NextActions = append(result.NextActions, NextAction{Action: "retry_target_duplicate_check", Parameters: map[string]any{"site_code": input.Target, "imdb_id": metadata.IMDbID}})
		return finalizeCandidateEvaluation(result)
	}
	if len(duplicate.Candidates) > 10 {
		duplicate.Candidates = append([]sites.TargetDuplicateCandidate(nil), duplicate.Candidates[:10]...)
		duplicate.CandidatesTruncated = true
	}
	result.DuplicateCheck = &duplicate
	if duplicate.Duplicate {
		result.Blockers = append(result.Blockers, Blocker{Code: "target_duplicate_detected", SiteCode: input.Target, Message: "target site already has one or more matching torrents"})
		result.NextActions = append(result.NextActions, NextAction{Action: "stop_duplicate_candidate", Parameters: map[string]any{"source_torrent_id": listed.TorrentID}})
	} else {
		result.Score += 25
		result.RecommendationReasons = append(result.RecommendationReasons, "target_duplicate_clear")
	}
	return finalizeCandidateEvaluation(result)
}

func finalizeCandidateEvaluation(result candidateEvaluation) candidateEvaluation {
	result.Ready = len(result.Blockers) == 0
	if result.Ready {
		result.RecommendationReasons = append(result.RecommendationReasons, "ready_for_retorrent_job")
	} else {
		result.Score -= float64(len(result.Blockers) * 20)
	}
	return result
}

func applyCandidatePolicy(result *candidateEvaluation, snapshot candidateRuleSite, role string) {
	policy := snapshot.Policy
	if !policy.Automation.Retorrent {
		result.Blockers = append(result.Blockers, Blocker{Code: "site_retorrent_forbidden", SiteCode: snapshot.SiteCode, Message: "active rules do not allow retorrenting"})
	}
	if role == "source" && !policy.Automation.Download {
		result.Blockers = append(result.Blockers, Blocker{Code: "site_download_forbidden", SiteCode: snapshot.SiteCode, Message: "active rules do not allow source downloads"})
	}
	if role == "target" && !policy.Automation.Upload {
		result.Blockers = append(result.Blockers, Blocker{Code: "site_upload_forbidden", SiteCode: snapshot.SiteCode, Message: "active rules do not allow target uploads"})
	}
	if role == "source" {
		if policy.Transfer.FreeleechRequired && !result.Source.Free {
			result.Blockers = append(result.Blockers, Blocker{Code: "freeleech_required", SiteCode: snapshot.SiteCode, Message: "source rules require a freeleech candidate"})
		}
		for _, required := range policy.Transfer.RequiredPromotions {
			if !containsFold(result.Source.PromotionLabels, required) {
				result.Blockers = append(result.Blockers, Blocker{Code: "required_promotion_missing", SiteCode: snapshot.SiteCode, Message: "candidate is missing required promotion " + required})
			}
		}
		for _, pattern := range policy.Transfer.ForbiddenTitlePatterns {
			compiled, err := regexp.Compile("(?i)" + pattern)
			if err != nil || compiled.MatchString(result.Source.Title) {
				message := "candidate title matches a forbidden rule pattern"
				if err != nil {
					message = "active rule contains an invalid forbidden title pattern"
				}
				result.Blockers = append(result.Blockers, Blocker{Code: "forbidden_title_pattern", SiteCode: snapshot.SiteCode, Message: message})
			}
		}
		for _, group := range policy.Transfer.ForbiddenReleaseGroups {
			if strings.Contains(strings.ToLower(result.Source.Title), strings.ToLower(strings.TrimSpace(group))) {
				result.Blockers = append(result.Blockers, Blocker{Code: "forbidden_release_group", SiteCode: snapshot.SiteCode, Message: "candidate title contains a forbidden release group"})
			}
		}
	}
	for _, obligation := range policy.Obligations {
		if obligation.Blocking && obligation.Verification == "manual" && obligation.Resolution != "not_applicable" {
			result.Risks = append(result.Risks, Blocker{Code: "manual_rule_obligation_required", SiteCode: snapshot.SiteCode, ObligationID: obligation.ID, Message: obligation.Description})
			result.NextActions = append(result.NextActions, NextAction{Action: "review_manual_rule_obligation", Parameters: map[string]any{"site_code": snapshot.SiteCode, "obligation_id": obligation.ID, "fingerprint": snapshot.Fingerprint}})
		}
	}
	if policy.Automation.ManualReviewRequired {
		result.Risks = append(result.Risks, Blocker{Code: "site_manual_review_required", SiteCode: snapshot.SiteCode, Message: "active site rules require human review before live execution"})
	}
}

type candidateRankExecutor struct{ dependencies candidateDependencies }

type rankedCandidate struct {
	CandidateID string              `json:"candidate_id"`
	Rank        *int                `json:"rank,omitempty"`
	Evaluation  candidateEvaluation `json:"evaluation"`
	Action      map[string]any      `json:"retorrent_action"`
}

type candidateDigest struct {
	SchemaVersion int               `json:"schema_version"`
	Kind          string            `json:"kind"`
	JobID         string            `json:"job_id"`
	Date          string            `json:"date"`
	Source        string            `json:"source"`
	Target        string            `json:"target"`
	TargetCount   int               `json:"target_count"`
	SelectedCount int               `json:"selected_count"`
	ReadyCount    int               `json:"ready_count"`
	TargetMet     bool              `json:"target_met"`
	Items         []rankedCandidate `json:"items"`
	GeneratedAt   time.Time         `json:"generated_at"`
}

func (executor candidateRankExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.dependencies.repository == nil || !candidateArtifactsReady(executor.dependencies) {
		return nil, fmt.Errorf("candidate ranking dependencies are unavailable")
	}
	input, err := candidateInput(execution.Job.Input)
	if err != nil {
		return nil, candidateInputBlock(err)
	}
	batch, err := loadCandidateEvaluation(ctx, execution, executor.dependencies.artifacts)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	slices.SortStableFunc(batch.Evaluations, func(left, right candidateEvaluation) int {
		if left.Ready != right.Ready {
			if left.Ready {
				return -1
			}
			return 1
		}
		if left.Score > right.Score {
			return -1
		}
		if left.Score < right.Score {
			return 1
		}
		return strings.Compare(left.Source.TorrentID, right.Source.TorrentID)
	})
	now := candidateNow(executor.dependencies)
	date := candidateDate(input.Date, now)
	digest := candidateDigest{
		SchemaVersion: 1, Kind: "upload-assistant.daily-candidates.v1", JobID: execution.Job.ID,
		Date: date.Format("2006-01-02"), Source: input.Source, Target: input.Target,
		TargetCount: input.TargetCount, Items: make([]rankedCandidate, 0, len(batch.Evaluations)), GeneratedAt: now,
	}
	nextRank := 1
	for _, evaluation := range batch.Evaluations {
		var rank *int
		status := candidates.StatusBlocked
		if evaluation.Ready {
			digest.ReadyCount++
			status = candidates.StatusCandidate
			if nextRank <= input.TargetCount {
				value := nextRank
				rank = &value
				nextRank++
				digest.SelectedCount++
			}
		}
		payload := map[string]any{
			"source": evaluation.Source, "metadata": evaluation.Metadata,
			"duplicate_check": evaluation.DuplicateCheck, "ready": evaluation.Ready,
			"score": evaluation.Score, "recommendation_reasons": evaluation.RecommendationReasons,
			"risks": evaluation.Risks, "blockers": evaluation.Blockers, "next_actions": evaluation.NextActions,
		}
		payloadBody, _ := json.Marshal(payload)
		stored, err := executor.dependencies.repository.Upsert(ctx, candidates.UpsertInput{
			DiscoveryJobID: execution.Job.ID, SourceSite: input.Source, TargetSite: input.Target,
			SourceTorrentID: evaluation.Source.TorrentID, RecommendationDate: date, Rank: rank,
			Score: evaluation.Score, Payload: payloadBody, Status: status, ExpiresAt: now.Add(48 * time.Hour),
		})
		if err != nil {
			return nil, fmt.Errorf("persist ranked candidate %s: %w", evaluation.Source.TorrentID, err)
		}
		action := map[string]any{
			"method": "POST", "path": "/api/v2/candidates/" + stored.ID + "/retorrent-job",
			"requires": []string{"Idempotency-Key", "explicit rule acceptance before live execution", "explicit confirm_upload before live upload"},
		}
		digest.Items = append(digest.Items, rankedCandidate{CandidateID: stored.ID, Rank: rank, Evaluation: evaluation, Action: action})
	}
	digest.TargetMet = digest.SelectedCount >= input.TargetCount
	artifact, err := persistCandidateArtifact(ctx, execution, executor.dependencies, "candidate_digest", "daily-candidates.json", digest)
	if err != nil {
		return nil, err
	}
	return mustJSON(map[string]any{
		"date": digest.Date, "target_count": digest.TargetCount, "selected_count": digest.SelectedCount,
		"ready_count": digest.ReadyCount, "target_met": digest.TargetMet, "digest_artifact": artifact,
	}), nil
}

type candidateSummaryExecutor struct{ dependencies candidateDependencies }

func (executor candidateSummaryExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if !candidateArtifactsReady(executor.dependencies) {
		return nil, fmt.Errorf("candidate summary dependencies are unavailable")
	}
	var frozen frozenStepInputs
	if err := json.Unmarshal(execution.Step.InputSnapshot, &frozen); err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	var ranked struct {
		Date           string                       `json:"date"`
		TargetCount    int                          `json:"target_count"`
		SelectedCount  int                          `json:"selected_count"`
		ReadyCount     int                          `json:"ready_count"`
		TargetMet      bool                         `json:"target_met"`
		DigestArtifact sites.TargetArtifactEvidence `json:"digest_artifact"`
	}
	if !decodePrevious(frozen.PreviousSteps, "candidate_rank", &ranked) {
		return nil, invalidSnapshotBlock(errors.New("candidate_rank output is missing"))
	}
	input, err := candidateInput(execution.Job.Input)
	if err != nil {
		return nil, candidateInputBlock(err)
	}
	status := "complete"
	blockers := []Blocker{}
	nextActions := []NextAction{{Action: "review_daily_candidates", Parameters: map[string]any{"date": ranked.Date, "source": input.Source, "target": input.Target}}}
	if !ranked.TargetMet {
		status = "complete_with_shortfall"
		blockers = append(blockers, Blocker{Code: "daily_candidate_shortfall", Message: fmt.Sprintf("selected %d of requested %d ready candidates", ranked.SelectedCount, ranked.TargetCount)})
		nextActions = append(nextActions, NextAction{Action: "scan_next_candidate_page", Parameters: map[string]any{"source": input.Source, "target": input.Target, "page": input.Page + 1}})
	}
	summary := map[string]any{
		"schema_version": 1, "kind": "upload-assistant.daily-candidate-summary.v1",
		"ok": ranked.TargetMet, "status": status, "job_id": execution.Job.ID,
		"date": ranked.Date, "source": input.Source, "target": input.Target,
		"target_count": ranked.TargetCount, "selected_count": ranked.SelectedCount,
		"ready_count": ranked.ReadyCount, "target_met": ranked.TargetMet,
		"blockers": blockers, "next_actions": nextActions, "digest_file": ranked.DigestArtifact,
		"generated_at": candidateNow(executor.dependencies),
	}
	artifact, err := persistCandidateArtifact(ctx, execution, executor.dependencies, "candidate_summary", "daily-candidate-summary.json", summary)
	if err != nil {
		return nil, err
	}
	summary["summary_file"] = artifact
	return mustJSON(summary), nil
}

func loadCandidateInputs(ctx context.Context, execution Execution, store ArtifactReader) (candidateRuleSnapshot, sites.CandidateScanEvidence, error) {
	_ = ctx
	var frozen frozenStepInputs
	if err := json.Unmarshal(execution.Step.InputSnapshot, &frozen); err != nil {
		return candidateRuleSnapshot{}, sites.CandidateScanEvidence{}, err
	}
	var rulesOutput struct {
		Rules candidateRuleSnapshot `json:"rules"`
	}
	if !decodePrevious(frozen.PreviousSteps, "candidate_rules", &rulesOutput) {
		return candidateRuleSnapshot{}, sites.CandidateScanEvidence{}, errors.New("candidate_rules output is missing")
	}
	var scanOutput struct {
		ScanArtifact sites.TargetArtifactEvidence `json:"scan_artifact"`
	}
	if !decodePrevious(frozen.PreviousSteps, "candidate_scan", &scanOutput) {
		return candidateRuleSnapshot{}, sites.CandidateScanEvidence{}, errors.New("candidate_scan output is missing")
	}
	body, err := readTargetArtifact(store, scanOutput.ScanArtifact, maxCandidateArtifactBytes)
	if err != nil {
		return candidateRuleSnapshot{}, sites.CandidateScanEvidence{}, fmt.Errorf("read candidate scan artifact: %w", err)
	}
	var scan sites.CandidateScanEvidence
	if err := json.Unmarshal(body, &scan); err != nil {
		return candidateRuleSnapshot{}, sites.CandidateScanEvidence{}, fmt.Errorf("decode candidate scan artifact: %w", err)
	}
	return rulesOutput.Rules, scan, nil
}

func loadCandidateEvaluation(ctx context.Context, execution Execution, store ArtifactReader) (candidateEvaluationBatch, error) {
	_ = ctx
	var frozen frozenStepInputs
	if err := json.Unmarshal(execution.Step.InputSnapshot, &frozen); err != nil {
		return candidateEvaluationBatch{}, err
	}
	var output struct {
		EvaluationArtifact sites.TargetArtifactEvidence `json:"evaluation_artifact"`
	}
	if !decodePrevious(frozen.PreviousSteps, "candidate_evaluate", &output) {
		return candidateEvaluationBatch{}, errors.New("candidate_evaluate output is missing")
	}
	body, err := readTargetArtifact(store, output.EvaluationArtifact, maxCandidateArtifactBytes)
	if err != nil {
		return candidateEvaluationBatch{}, fmt.Errorf("read candidate evaluation artifact: %w", err)
	}
	var batch candidateEvaluationBatch
	if err := json.Unmarshal(body, &batch); err != nil {
		return candidateEvaluationBatch{}, fmt.Errorf("decode candidate evaluation artifact: %w", err)
	}
	return batch, nil
}

func persistCandidateArtifact(ctx context.Context, execution Execution, dependencies candidateDependencies, kind, filename string, value any) (sites.TargetArtifactEvidence, error) {
	body, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("serialize %s artifact: %w", kind, err)
	}
	if len(body) > maxCandidateArtifactBytes {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("%s artifact exceeds the size limit", kind)
	}
	file, err := dependencies.artifacts.Write(ctx, artifacts.Scope{JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID}, filename, bytes.NewReader(body))
	if err != nil {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("persist %s artifact: %w", kind, err)
	}
	recorded, err := dependencies.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: kind, StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{"schema_version": 1}), Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("register %s artifact: %w", kind, err)
	}
	return sites.TargetArtifactEvidence{ArtifactID: recorded.ID, StoragePath: recorded.StoragePath, SHA256: recorded.SHA256, SizeBytes: recorded.SizeBytes}, nil
}

func candidateArtifactsReady(dependencies candidateDependencies) bool {
	return dependencies.artifacts != nil && dependencies.recorder != nil
}

func candidateInputBlock(err error) *BlockError {
	return &BlockError{
		Blockers:    []Blocker{{Code: "invalid_candidate_job_input", Message: err.Error()}},
		NextActions: []NextAction{{Action: "create_candidate_job_with_valid_input"}},
		ResumeState: map[string]any{},
	}
}

func candidateProviderBlock(err error, siteCode, operation string) *BlockError {
	code, message, temporary := sites.ErrorDetails(err)
	return &BlockError{
		Blockers:    []Blocker{{Code: code, Message: message, SiteCode: siteCode}},
		NextActions: []NextAction{{Action: "configure_or_retry_candidate_source", Parameters: map[string]any{"site_code": siteCode, "operation": operation}}},
		ResumeState: map[string]any{"retryable": temporary, "site_code": siteCode, "operation": operation},
	}
}

func candidateDate(value string, now time.Time) time.Time {
	if value != "" {
		parsed, _ := time.Parse("2006-01-02", value)
		return parsed
	}
	location := time.FixedZone("Asia/Shanghai", 8*60*60)
	local := now.In(location)
	return time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, time.UTC)
}

func candidateNow(dependencies candidateDependencies) time.Time {
	if dependencies.now != nil {
		return dependencies.now().UTC()
	}
	return time.Now().UTC()
}

func containsFold(values []string, expected string) bool {
	for _, value := range values {
		if strings.EqualFold(strings.TrimSpace(value), strings.TrimSpace(expected)) {
			return true
		}
	}
	return false
}
