package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type TargetDuplicateChecker interface {
	DuplicateCheck(context.Context, string, sites.TargetDuplicateQuery, workflow.Actor) (sites.TargetDuplicateEvidence, error)
}

func WithTargetDuplicateChecks(checker TargetDuplicateChecker, artifactStore WorkflowArtifactStore) Option {
	return func(runner *Runner) {
		runner.executors["target_duplicate_check"] = targetDuplicateExecutor{
			checker: checker, artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type targetDuplicateExecutor struct {
	checker   TargetDuplicateChecker
	artifacts WorkflowArtifactStore
	recorder  ArtifactRecorder
}

type duplicateCheckDocument struct {
	SchemaVersion int                           `json:"schema_version"`
	TargetPackage sites.TargetArtifactEvidence  `json:"target_package"`
	Evidence      sites.TargetDuplicateEvidence `json:"evidence"`
}

func (executor targetDuplicateExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.checker == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("target duplicate-check workflow dependencies are unavailable")
	}
	query, packageEvidence, target, err := executor.inputs(execution.Step.InputSnapshot)
	if err != nil {
		var blocked *BlockError
		if errors.As(err, &blocked) {
			return nil, blocked
		}
		return nil, invalidSnapshotBlock(err)
	}
	evidence, err := executor.checker.DuplicateCheck(ctx, target, query, execution.Actor)
	if err != nil {
		return nil, targetDuplicateRequestBlock(err, target, query)
	}
	if err := validateTargetDuplicateEvidence(evidence, target, query); err != nil {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: "target_duplicate_evidence_invalid", Message: err.Error(), SiteCode: target}},
			NextActions: []NextAction{{Action: "retry_target_duplicate_check", Description: "Retry with a target adapter that returns evidence bound to the requested site and identity."}},
			ResumeState: map[string]any{"target_duplicate_check": query},
		}
	}
	document := duplicateCheckDocument{SchemaVersion: 1, TargetPackage: packageEvidence, Evidence: evidence}
	body, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize target duplicate-check evidence: %w", err)
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(target)+"-duplicate-check.json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("persist target duplicate-check evidence: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "duplicate_check", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": target, "imdb_id": query.IMDbID, "duplicate": evidence.Duplicate,
			"result_count": evidence.ResultCount, "target_package_sha256": packageEvidence.SHA256,
			"configuration_sha256": evidence.ConfigurationSHA256,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register target duplicate-check evidence: %w", err)
	}
	result := map[string]any{
		"checked": true, "status": "clean", "target": target, "duplicate": evidence.Duplicate,
		"query": query, "result_count": evidence.ResultCount, "candidates": evidence.Candidates,
		"candidates_truncated": evidence.CandidatesTruncated, "checked_at": evidence.CheckedAt,
		"configuration_sha256":        evidence.ConfigurationSHA256,
		"duplicate_check_artifact_id": recorded.ID, "duplicate_check_sha256": recorded.SHA256,
		"duplicate_check_storage_path": recorded.StoragePath,
		"target_package_artifact_id":   packageEvidence.ArtifactID, "target_package_sha256": packageEvidence.SHA256,
	}
	if evidence.Duplicate {
		result["status"] = "duplicate"
		return nil, &BlockError{
			Blockers: []Blocker{{
				Code: "target_duplicate_detected", SiteCode: target,
				Message: fmt.Sprintf("%s returned %d potential duplicate torrent(s); the upload workflow is stopped", target, evidence.ResultCount),
			}},
			NextActions: []NextAction{{
				Action:      "review_duplicate_candidates",
				Description: "Review the immutable duplicate-check artifact and stop or cancel this job; resuming performs a fresh check and never bypasses a match.",
				Parameters:  map[string]any{"artifact_id": recorded.ID, "candidates": evidence.Candidates},
			}},
			ResumeState: map[string]any{"duplicate_check": result},
		}
	}
	return mustJSON(result), nil
}

func validateTargetDuplicateEvidence(evidence sites.TargetDuplicateEvidence, target string, query sites.TargetDuplicateQuery) error {
	if evidence.SiteCode != target || evidence.Query.IMDbID != query.IMDbID {
		return fmt.Errorf("target duplicate-check evidence is not bound to the requested site and IMDb identity")
	}
	if len(evidence.ConfigurationSHA256) != 64 || evidence.CheckedAt.IsZero() {
		return fmt.Errorf("target duplicate-check evidence has no configuration revision or check time")
	}
	if evidence.ResultCount < len(evidence.Candidates) || evidence.Duplicate != (evidence.ResultCount > 0) {
		return fmt.Errorf("target duplicate-check result count and duplicate decision are inconsistent")
	}
	return nil
}

func (executor targetDuplicateExecutor) inputs(snapshotBody json.RawMessage) (sites.TargetDuplicateQuery, sites.TargetArtifactEvidence, string, error) {
	var snapshot struct {
		JobInput struct {
			TargetDuplicateCheck sites.TargetDuplicateQuery `json:"target_duplicate_check"`
		} `json:"job_input"`
		ResumeState struct {
			TargetDuplicateCheck sites.TargetDuplicateQuery `json:"target_duplicate_check"`
		} `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return sites.TargetDuplicateQuery{}, sites.TargetArtifactEvidence{}, "", fmt.Errorf("decode target duplicate-check snapshot: %w", err)
	}
	var packageOutput struct {
		Prepared           bool   `json:"prepared"`
		Target             string `json:"target"`
		PackageArtifactID  string `json:"package_artifact_id"`
		PackageSHA256      string `json:"package_sha256"`
		PackageStoragePath string `json:"package_storage_path"`
		PackageSizeBytes   int64  `json:"package_size_bytes"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_package", &packageOutput) || !packageOutput.Prepared || packageOutput.Target == "" ||
		packageOutput.PackageArtifactID == "" || packageOutput.PackageSHA256 == "" || packageOutput.PackageStoragePath == "" {
		return sites.TargetDuplicateQuery{}, sites.TargetArtifactEvidence{}, "", fmt.Errorf("target_package evidence is missing or incomplete")
	}
	packageEvidence := sites.TargetArtifactEvidence{
		ArtifactID: packageOutput.PackageArtifactID, StoragePath: packageOutput.PackageStoragePath,
		SHA256: packageOutput.PackageSHA256, SizeBytes: packageOutput.PackageSizeBytes,
	}
	body, err := readTargetArtifact(executor.artifacts, packageEvidence, maxTargetPackageArtifact)
	if err != nil {
		return sites.TargetDuplicateQuery{}, sites.TargetArtifactEvidence{}, "", fmt.Errorf("target package artifact verification failed")
	}
	var prepared sites.PreparedTargetPackage
	if json.Unmarshal(body, &prepared) != nil || prepared.Target != packageOutput.Target || prepared.SchemaVersion != 1 {
		return sites.TargetDuplicateQuery{}, sites.TargetArtifactEvidence{}, "", fmt.Errorf("target package artifact is invalid or mismatched")
	}
	packageIMDbID := imdbFromPackage(prepared)
	query := snapshot.JobInput.TargetDuplicateCheck
	if resumed := strings.TrimSpace(snapshot.ResumeState.TargetDuplicateCheck.IMDbID); resumed != "" {
		query.IMDbID = resumed
	}
	query.IMDbID = strings.ToLower(strings.TrimSpace(query.IMDbID))
	if packageIMDbID != "" {
		if query.IMDbID != "" && query.IMDbID != packageIMDbID {
			return sites.TargetDuplicateQuery{}, packageEvidence, packageOutput.Target, &BlockError{
				Blockers: []Blocker{{
					Code: "target_duplicate_identity_conflict", SiteCode: packageOutput.Target,
					Message: "the explicit duplicate-check IMDb id conflicts with the immutable target package identity",
				}},
				NextActions: []NextAction{{
					Action: "review_target_identity", Description: "Correct the upstream metadata or provide the same IMDb id as the target package before resuming.",
					Parameters: map[string]any{"package_imdb_id": packageIMDbID, "provided_imdb_id": query.IMDbID},
				}},
				ResumeState: map[string]any{"target_duplicate_check": query, "target_package_sha256": packageEvidence.SHA256},
			}
		}
		query.IMDbID = packageIMDbID
	}
	if !targetIMDbPattern.MatchString(query.IMDbID) {
		return sites.TargetDuplicateQuery{}, packageEvidence, packageOutput.Target, &BlockError{
			Blockers: []Blocker{{Code: "target_duplicate_identity_required", Message: packageOutput.Target + " duplicate check requires an IMDb id in tt1234567 form", SiteCode: packageOutput.Target}},
			NextActions: []NextAction{{
				Action: "provide_duplicate_identity", Description: "Provide a reviewed IMDb id in resume_state.target_duplicate_check.imdb_id and resume.",
				Parameters: map[string]any{"field": "target_duplicate_check.imdb_id", "pattern": "^tt[0-9]{5,12}$"},
			}},
			ResumeState: map[string]any{"target_duplicate_check": query, "target_package_sha256": packageEvidence.SHA256},
		}
	}
	return query, packageEvidence, packageOutput.Target, nil
}

var targetIMDbPattern = regexp.MustCompile(`^tt[0-9]{5,12}$`)

func imdbFromPackage(prepared sites.PreparedTargetPackage) string {
	value := strings.TrimSpace(prepared.MetadataLinks["imdb"])
	if value == "" {
		value, _ = prepared.FormFields["imdb"].(string)
	}
	if targetIMDbPattern.MatchString(strings.ToLower(value)) {
		return strings.ToLower(value)
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed.Hostname() != "www.imdb.com" {
		return ""
	}
	for _, segment := range strings.Split(parsed.Path, "/") {
		segment = strings.ToLower(strings.TrimSpace(segment))
		if targetIMDbPattern.MatchString(segment) {
			return segment
		}
	}
	return ""
}

func targetDuplicateRequestBlock(err error, target string, query sites.TargetDuplicateQuery) *BlockError {
	code, message, _ := sites.ErrorDetails(err)
	action := "retry_target_duplicate_check"
	description := "Verify target-site availability and retry this independently resumable duplicate check."
	switch code {
	case "site_api_key_required", "site_authentication_failed", "site_configuration_unavailable", "site_configuration_invalid":
		action = "configure_target_site"
		description = "Configure or refresh the encrypted target-site credential and endpoint before resuming."
	case "target_duplicate_identity_required":
		action = "provide_duplicate_identity"
		description = "Provide a reviewed IMDb identity before resuming."
	case "site_adapter_mismatch", "target_duplicate_adapter_unavailable":
		action = "configure_target_adapter"
		description = "Enable a target duplicate-check adapter before resuming."
	}
	return &BlockError{
		Blockers:    []Blocker{{Code: code, Message: message, SiteCode: target}},
		NextActions: []NextAction{{Action: action, Description: description, Parameters: map[string]any{"site_code": target}}},
		ResumeState: map[string]any{"target_duplicate_check": query},
	}
}
