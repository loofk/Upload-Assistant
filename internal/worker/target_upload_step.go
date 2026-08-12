package worker

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type TargetUploader interface {
	Upload(context.Context, string, sites.TargetUploadRequest, workflow.Actor) (sites.TargetUploadEvidence, error)
}

func WithTargetUploads(uploader TargetUploader, duplicateChecker TargetDuplicateChecker, ruleProvider RuleProvider, artifactStore WorkflowArtifactStore) Option {
	return func(runner *Runner) {
		runner.executors["target_upload"] = targetUploadExecutor{
			uploader: uploader, duplicates: duplicateChecker, rules: ruleProvider,
			artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type targetUploadExecutor struct {
	uploader   TargetUploader
	duplicates TargetDuplicateChecker
	rules      RuleProvider
	artifacts  WorkflowArtifactStore
	recorder   ArtifactRecorder
}

type targetUploadBindings struct {
	Target                string
	Confirmed             bool
	Package               sites.PreparedTargetPackage
	PackageArtifact       sites.TargetArtifactEvidence
	PriorDuplicate        sites.TargetArtifactEvidence
	PriorDuplicateQuery   sites.TargetDuplicateQuery
	TargetTorrent         []byte
	TargetTorrentArtifact sites.TargetArtifactEvidence
	TargetTorrentReceipt  sites.TargetArtifactEvidence
	TorrentInspection     torrentmeta.Inspection
	RuleRevisionID        string
	RuleFingerprint       string
	RuleAcceptanceSHA     string
	Reconciliation        targetUploadReconciliation
}

type targetUploadReconciliation struct {
	BlockerCode            string `json:"blocker_code"`
	AttemptID              string `json:"attempt_id"`
	Decision               string `json:"decision"`
	Confirmed              bool   `json:"confirmed"`
	EvidenceSHA256         string `json:"evidence_sha256"`
	ObservedAt             string `json:"observed_at"`
	ObservedTorrentID      string `json:"observed_torrent_id"`
	SubmittedTorrentSHA256 string `json:"submitted_torrent_sha256"`
}

type preuploadDuplicateDocument struct {
	SchemaVersion        int                           `json:"schema_version"`
	TargetPackageSHA256  string                        `json:"target_package_sha256"`
	TargetTorrentSHA256  string                        `json:"target_torrent_sha256"`
	PriorDuplicateSHA256 string                        `json:"prior_duplicate_check_sha256"`
	RuleFingerprint      string                        `json:"rule_fingerprint"`
	Evidence             sites.TargetDuplicateEvidence `json:"evidence"`
}

type targetUploadReceipt struct {
	SchemaVersion  int                          `json:"schema_version"`
	Target         string                       `json:"target"`
	Confirmation   targetUploadConfirmation     `json:"confirmation"`
	CurrentRule    targetUploadRuleBinding      `json:"current_rule"`
	Package        sites.TargetArtifactEvidence `json:"target_package"`
	Torrent        sites.TargetArtifactEvidence `json:"target_torrent"`
	TorrentReceipt sites.TargetArtifactEvidence `json:"target_torrent_receipt"`
	PriorDuplicate sites.TargetArtifactEvidence `json:"prior_duplicate_check"`
	FreshDuplicate sites.TargetArtifactEvidence `json:"preupload_duplicate_check"`
	Upload         sites.TargetUploadEvidence   `json:"upload"`
	Reconciliation *targetUploadRecoveryReceipt `json:"reconciliation,omitempty"`
}

type targetUploadRecoveryReceipt struct {
	Recovered              bool      `json:"recovered"`
	Decision               string    `json:"decision"`
	AttemptID              string    `json:"attempt_id"`
	EvidenceSHA256         string    `json:"evidence_sha256"`
	ObservedAt             time.Time `json:"observed_at"`
	ObservedTorrentID      string    `json:"observed_torrent_id"`
	SubmittedTorrentSHA256 string    `json:"submitted_torrent_sha256"`
}

type targetUploadConfirmation struct {
	Confirmed bool           `json:"confirmed"`
	Actor     workflow.Actor `json:"actor"`
	BoundAt   time.Time      `json:"bound_at"`
}

type targetUploadRuleBinding struct {
	RevisionID    string `json:"revision_id"`
	Fingerprint   string `json:"fingerprint"`
	AcceptanceSHA string `json:"acceptance_sha256"`
}

func (executor targetUploadExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.uploader == nil || executor.duplicates == nil || executor.rules == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("target upload workflow dependencies are unavailable")
	}
	bindings, err := executor.inputs(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	if bindings.Reconciliation.Decision == "verified_uploaded" {
		return executor.recoverUploaded(ctx, execution, bindings)
	}
	if !bindings.Confirmed {
		return nil, &BlockError{
			Blockers: []Blocker{{
				Code: "confirm_upload_required", SiteCode: bindings.Target,
				Message: "live target upload requires an explicit confirm_upload=true value",
			}},
			NextActions: []NextAction{{
				Action: "confirm_live_upload", Description: "Review the immutable package, duplicate checks, target torrent receipt, and current rules; then set confirm_upload=true and resume.",
				Parameters: map[string]any{
					"field": "confirm_upload", "target_package_sha256": bindings.PackageArtifact.SHA256,
					"target_torrent_sha256": bindings.TargetTorrentArtifact.SHA256, "rule_fingerprint": bindings.RuleFingerprint,
				},
			}},
			ResumeState: map[string]any{"confirm_upload": false},
		}
	}
	if blocked := executor.currentRuleBlock(ctx, bindings); blocked != nil {
		return nil, blocked
	}

	freshDuplicate, err := executor.duplicates.DuplicateCheck(ctx, bindings.Target, bindings.PriorDuplicateQuery, execution.Actor)
	if err != nil {
		if deferred := deferredSiteAccess(err); deferred != nil {
			return nil, deferred
		}
		return nil, targetDuplicateRequestBlock(err, bindings.Target, bindings.PriorDuplicateQuery)
	}
	if err := validateTargetDuplicateEvidence(freshDuplicate, bindings.Target, bindings.PriorDuplicateQuery); err != nil {
		return nil, targetUploadEvidenceBlock("preupload_duplicate_evidence_invalid", err.Error(), bindings)
	}
	freshDuplicateArtifact, err := executor.persistPreuploadDuplicate(ctx, execution, bindings, freshDuplicate)
	if err != nil {
		return nil, err
	}
	if freshDuplicate.Duplicate {
		return nil, &BlockError{
			Blockers: []Blocker{{
				Code: "target_duplicate_detected", SiteCode: bindings.Target,
				Message: fmt.Sprintf("the final pre-upload check found %d potential duplicate torrent(s); no upload was attempted", freshDuplicate.ResultCount),
			}},
			NextActions: []NextAction{{
				Action: "review_duplicate_candidates", Description: "Review the final duplicate evidence and stop or reconcile this job; resuming performs another fresh check and never bypasses a match.",
				Parameters: map[string]any{"artifact_id": freshDuplicateArtifact.ArtifactID, "candidates": freshDuplicate.Candidates},
			}},
			ResumeState: map[string]any{"target_upload": map[string]any{
				"preupload_duplicate_check": freshDuplicateArtifact, "confirm_upload": true,
			}},
		}
	}
	upload, err := executor.uploader.Upload(ctx, bindings.Target, sites.TargetUploadRequest{
		JobID: execution.Job.ID, AttemptID: execution.Attempt.ID, Confirmed: true,
		Package: bindings.Package, Torrent: bindings.TargetTorrent,
		PackageSHA256: bindings.PackageArtifact.SHA256, TorrentSHA256: bindings.TargetTorrentArtifact.SHA256,
		ContentFingerprintSHA256: bindings.TorrentInspection.ContentFingerprint,
		RuleFingerprint:          bindings.RuleFingerprint, DuplicateCheckSHA256: freshDuplicateArtifact.SHA256,
	}, execution.Actor)
	if err != nil {
		if deferred := deferredSiteAccess(err); deferred != nil {
			return nil, deferred
		}
		return nil, targetUploadAdapterBlock(err, bindings, freshDuplicateArtifact)
	}
	if err := validateTargetUploadEvidence(upload, bindings); err != nil {
		return nil, targetUploadPostWriteBlock("target upload returned invalid success evidence: "+err.Error(), bindings, freshDuplicateArtifact, upload)
	}
	output, err := executor.persistUploadReceipt(ctx, execution, bindings, freshDuplicateArtifact, upload, nil)
	if err != nil {
		return nil, targetUploadPostWriteBlock("target upload succeeded but its immutable receipt could not be persisted: "+err.Error(), bindings, freshDuplicateArtifact, upload)
	}
	return output, nil
}

func (executor targetUploadExecutor) persistUploadReceipt(
	ctx context.Context,
	execution Execution,
	bindings targetUploadBindings,
	freshDuplicateArtifact sites.TargetArtifactEvidence,
	upload sites.TargetUploadEvidence,
	recovery *targetUploadRecoveryReceipt,
) (json.RawMessage, error) {
	receipt := targetUploadReceipt{
		SchemaVersion: 1, Target: bindings.Target,
		Confirmation: targetUploadConfirmation{Confirmed: true, Actor: execution.Actor, BoundAt: time.Now().UTC()},
		CurrentRule: targetUploadRuleBinding{
			RevisionID: bindings.RuleRevisionID, Fingerprint: bindings.RuleFingerprint, AcceptanceSHA: bindings.RuleAcceptanceSHA,
		},
		Package: bindings.PackageArtifact, Torrent: bindings.TargetTorrentArtifact,
		TorrentReceipt: bindings.TargetTorrentReceipt, PriorDuplicate: bindings.PriorDuplicate,
		FreshDuplicate: freshDuplicateArtifact, Upload: upload, Reconciliation: recovery,
	}
	body, err := json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize target upload receipt: %w", err)
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(bindings.Target)+"-upload-receipt.json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("persist target upload receipt: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "target_upload_receipt", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": bindings.Target, "torrent_id": upload.TorrentID, "details_url": upload.DetailsURL,
			"target_torrent_sha256":            bindings.TargetTorrentArtifact.SHA256,
			"preupload_duplicate_check_sha256": freshDuplicateArtifact.SHA256,
			"rule_fingerprint":                 bindings.RuleFingerprint, "configuration_sha256": upload.ConfigurationSHA256,
			"recovered": recovery != nil,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register target upload receipt: %w", err)
	}
	return mustJSON(map[string]any{
		"uploaded": true, "status": "uploaded", "target": bindings.Target,
		"recovered":           recovery != nil,
		"uploaded_torrent_id": upload.TorrentID, "details_url": upload.DetailsURL,
		"submitted_at": upload.SubmittedAt, "response_sha256": upload.ResponseSHA256,
		"configuration_sha256":                  upload.ConfigurationSHA256,
		"submitted_torrent_sha256":              bindings.TargetTorrentArtifact.SHA256,
		"submitted_infohashes":                  bindings.TorrentInspection.Hashes,
		"preupload_duplicate_check_artifact_id": freshDuplicateArtifact.ArtifactID,
		"preupload_duplicate_check_sha256":      freshDuplicateArtifact.SHA256,
		"upload_receipt_artifact_id":            recorded.ID, "upload_receipt_sha256": recorded.SHA256,
		"upload_receipt_storage_path": recorded.StoragePath,
	}), nil
}

func (executor targetUploadExecutor) recoverUploaded(
	ctx context.Context,
	execution Execution,
	bindings targetUploadBindings,
) (json.RawMessage, error) {
	reconciliation := bindings.Reconciliation
	observedAt, timeErr := time.Parse(time.RFC3339, reconciliation.ObservedAt)
	evidenceDigest, evidenceErr := hex.DecodeString(reconciliation.EvidenceSHA256)
	if reconciliation.BlockerCode != "target_upload_outcome_unknown" || reconciliation.AttemptID == "" ||
		!reconciliation.Confirmed || timeErr != nil || observedAt.IsZero() || evidenceErr != nil || len(evidenceDigest) != 32 ||
		reconciliation.SubmittedTorrentSHA256 != bindings.TargetTorrentArtifact.SHA256 ||
		!numericTorrentID(reconciliation.ObservedTorrentID) {
		return nil, targetUploadReconciliationBlock(
			"target_reconciliation_evidence_invalid",
			"verified_uploaded reconciliation is incomplete or is not bound to the immutable submitted torrent",
			bindings, nil,
		)
	}
	freshDuplicate, err := executor.duplicates.DuplicateCheck(ctx, bindings.Target, bindings.PriorDuplicateQuery, execution.Actor)
	if err != nil {
		if deferred := deferredSiteAccess(err); deferred != nil {
			return nil, deferred
		}
		code, message, _ := sites.ErrorDetails(err)
		return nil, targetUploadReconciliationBlock(code, message, bindings, nil)
	}
	if err := validateTargetDuplicateEvidence(freshDuplicate, bindings.Target, bindings.PriorDuplicateQuery); err != nil ||
		freshDuplicate.Adapter != bindings.Package.Adapter {
		if err == nil {
			err = fmt.Errorf("target reconciliation adapter does not match the immutable target package")
		}
		return nil, targetUploadReconciliationBlock("target_reconciliation_evidence_invalid", err.Error(), bindings, nil)
	}
	freshDuplicateArtifact, err := executor.persistPreuploadDuplicate(ctx, execution, bindings, freshDuplicate)
	if err != nil {
		return nil, targetUploadReconciliationBlock("target_reconciliation_persistence_failed", err.Error(), bindings, nil)
	}
	matched := false
	for _, candidate := range freshDuplicate.Candidates {
		if candidate.ID == reconciliation.ObservedTorrentID {
			matched = true
			break
		}
	}
	if !matched {
		return nil, targetUploadReconciliationBlock(
			"target_reconciliation_candidate_not_found",
			"the fresh target search did not contain the operator-confirmed torrent id; no upload was attempted",
			bindings, &freshDuplicateArtifact,
		)
	}
	upload := sites.TargetUploadEvidence{
		SiteCode: bindings.Target, Adapter: bindings.Package.Adapter,
		ConfigurationSHA256: freshDuplicate.ConfigurationSHA256,
		TorrentID:           reconciliation.ObservedTorrentID,
		DetailsURL:          "https://kp.m-team.cc/details/" + reconciliation.ObservedTorrentID,
		ResponseSHA256:      reconciliation.EvidenceSHA256,
		SubmittedAt:         observedAt.UTC(),
	}
	if err := validateTargetUploadEvidence(upload, bindings); err != nil {
		return nil, targetUploadReconciliationBlock("target_reconciliation_evidence_invalid", err.Error(), bindings, &freshDuplicateArtifact)
	}
	recovery := &targetUploadRecoveryReceipt{
		Recovered: true, Decision: reconciliation.Decision, AttemptID: reconciliation.AttemptID,
		EvidenceSHA256: reconciliation.EvidenceSHA256, ObservedAt: observedAt.UTC(),
		ObservedTorrentID: reconciliation.ObservedTorrentID, SubmittedTorrentSHA256: reconciliation.SubmittedTorrentSHA256,
	}
	output, err := executor.persistUploadReceipt(ctx, execution, bindings, freshDuplicateArtifact, upload, recovery)
	if err != nil {
		return nil, targetUploadReconciliationBlock("target_reconciliation_persistence_failed", err.Error(), bindings, &freshDuplicateArtifact)
	}
	return output, nil
}

func targetUploadReconciliationBlock(
	code, message string,
	bindings targetUploadBindings,
	artifact *sites.TargetArtifactEvidence,
) *BlockError {
	parameters := map[string]any{
		"site_code": bindings.Target, "torrent_id": bindings.Reconciliation.ObservedTorrentID,
		"submitted_torrent_sha256": bindings.TargetTorrentArtifact.SHA256,
	}
	if artifact != nil {
		parameters["artifact_id"] = artifact.ArtifactID
		parameters["artifact_sha256"] = artifact.SHA256
	}
	return &BlockError{
		Blockers: []Blocker{{Code: code, Message: message, SiteCode: bindings.Target}},
		NextActions: []NextAction{{
			Action: "review_target_reconciliation", Description: "Review the fresh target-search artifact and correct the reconciliation evidence before resuming; never retry the upload.",
			Parameters: parameters,
		}},
		ResumeState: map[string]any{"reconciliation": bindings.Reconciliation, "confirm_upload": false},
	}
}

func targetUploadPostWriteBlock(
	message string,
	bindings targetUploadBindings,
	freshDuplicate sites.TargetArtifactEvidence,
	upload sites.TargetUploadEvidence,
) *BlockError {
	parameters := map[string]any{
		"site_code": bindings.Target, "submitted_torrent_sha256": bindings.TargetTorrentArtifact.SHA256,
		"preupload_duplicate_check_sha256": freshDuplicate.SHA256,
	}
	if numericTorrentID(upload.TorrentID) {
		parameters["observed_torrent_id"] = upload.TorrentID
	}
	if len(upload.ResponseSHA256) == 64 {
		parameters["response_sha256"] = upload.ResponseSHA256
	}
	return &BlockError{
		Blockers: []Blocker{{Code: "target_upload_outcome_unknown", Message: message, SiteCode: bindings.Target}},
		NextActions: []NextAction{{
			Action: "reconcile_target_upload", Description: "Do not retry the upload. Inspect the target site and reconcile the exact submitted torrent before resuming.",
			Parameters: parameters,
		}},
		ResumeState: map[string]any{
			"target_upload": map[string]any{
				"outcome": "unreconciled", "submitted_torrent_sha256": bindings.TargetTorrentArtifact.SHA256,
				"preupload_duplicate_check": freshDuplicate, "observed_torrent_id": upload.TorrentID,
			},
			"confirm_upload": false,
		},
	}
}

func numericTorrentID(value string) bool {
	if value == "" || len(value) > 20 {
		return false
	}
	for _, character := range value {
		if character < '0' || character > '9' {
			return false
		}
	}
	return true
}

func (executor targetUploadExecutor) inputs(snapshotBody json.RawMessage) (targetUploadBindings, error) {
	var snapshot struct {
		JobInput struct {
			ConfirmUpload *bool `json:"confirm_upload"`
		} `json:"job_input"`
		ResumeState struct {
			ConfirmUpload  *bool                      `json:"confirm_upload"`
			Reconciliation targetUploadReconciliation `json:"reconciliation"`
		} `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return targetUploadBindings{}, fmt.Errorf("decode target upload snapshot: %w", err)
	}
	confirmed := snapshot.JobInput.ConfirmUpload != nil && *snapshot.JobInput.ConfirmUpload
	if snapshot.ResumeState.ConfirmUpload != nil {
		confirmed = *snapshot.ResumeState.ConfirmUpload
	}
	var parsed struct {
		Target string `json:"target"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "source_parse", &parsed) || parsed.Target == "" {
		return targetUploadBindings{}, fmt.Errorf("source_parse target evidence is missing")
	}
	bindings := targetUploadBindings{
		Target: strings.ToUpper(strings.TrimSpace(parsed.Target)), Confirmed: confirmed,
		Reconciliation: snapshot.ResumeState.Reconciliation,
	}

	var packageOutput struct {
		Prepared           bool   `json:"prepared"`
		Target             string `json:"target"`
		PackageArtifactID  string `json:"package_artifact_id"`
		PackageSHA256      string `json:"package_sha256"`
		PackageStoragePath string `json:"package_storage_path"`
		PackageSizeBytes   int64  `json:"package_size_bytes"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_package", &packageOutput) || !packageOutput.Prepared || packageOutput.Target != bindings.Target ||
		packageOutput.PackageArtifactID == "" || packageOutput.PackageSHA256 == "" || packageOutput.PackageStoragePath == "" {
		return targetUploadBindings{}, fmt.Errorf("target_package evidence is missing")
	}
	bindings.PackageArtifact = sites.TargetArtifactEvidence{
		ArtifactID: packageOutput.PackageArtifactID, StoragePath: packageOutput.PackageStoragePath,
		SHA256: packageOutput.PackageSHA256, SizeBytes: packageOutput.PackageSizeBytes,
	}
	packageBody, err := readTargetArtifact(executor.artifacts, bindings.PackageArtifact, maxTargetPackageArtifact)
	if err != nil || json.Unmarshal(packageBody, &bindings.Package) != nil || bindings.Package.SchemaVersion != 1 ||
		bindings.Package.Target != bindings.Target || bindings.Package.Adapter == "" {
		return targetUploadBindings{}, fmt.Errorf("target package artifact verification failed")
	}

	var duplicateOutput struct {
		Checked     bool   `json:"checked"`
		Status      string `json:"status"`
		Target      string `json:"target"`
		Duplicate   bool   `json:"duplicate"`
		ArtifactID  string `json:"duplicate_check_artifact_id"`
		SHA256      string `json:"duplicate_check_sha256"`
		StoragePath string `json:"duplicate_check_storage_path"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_duplicate_check", &duplicateOutput) || !duplicateOutput.Checked ||
		duplicateOutput.Status != "clean" || duplicateOutput.Duplicate || duplicateOutput.Target != bindings.Target ||
		duplicateOutput.ArtifactID == "" || duplicateOutput.SHA256 == "" || duplicateOutput.StoragePath == "" {
		return targetUploadBindings{}, fmt.Errorf("clean target_duplicate_check evidence is missing")
	}
	bindings.PriorDuplicate = sites.TargetArtifactEvidence{
		ArtifactID: duplicateOutput.ArtifactID, StoragePath: duplicateOutput.StoragePath, SHA256: duplicateOutput.SHA256,
	}
	duplicateBody, err := readTargetArtifact(executor.artifacts, bindings.PriorDuplicate, maxTargetPackageArtifact)
	var duplicateDocument duplicateCheckDocument
	if err != nil || json.Unmarshal(duplicateBody, &duplicateDocument) != nil || duplicateDocument.SchemaVersion != 1 ||
		duplicateDocument.Evidence.Duplicate || duplicateDocument.Evidence.SiteCode != bindings.Target ||
		duplicateDocument.TargetPackage.SHA256 != bindings.PackageArtifact.SHA256 || duplicateDocument.Evidence.Query.IMDbID == "" {
		return targetUploadBindings{}, fmt.Errorf("prior duplicate-check artifact verification failed")
	}
	bindings.PriorDuplicateQuery = duplicateDocument.Evidence.Query

	var ruleOutput struct {
		SiteCode      string `json:"site_code"`
		Role          string `json:"role"`
		RevisionID    string `json:"rule_revision_id"`
		Fingerprint   string `json:"fingerprint"`
		Accepted      bool   `json:"accepted"`
		AcceptanceSHA string `json:"acceptance_sha256"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_rules", &ruleOutput) || ruleOutput.SiteCode != bindings.Target || ruleOutput.Role != "target" ||
		!ruleOutput.Accepted || ruleOutput.RevisionID == "" || len(ruleOutput.Fingerprint) != 64 || len(ruleOutput.AcceptanceSHA) != 64 {
		return targetUploadBindings{}, fmt.Errorf("accepted target_rules evidence is missing")
	}
	bindings.RuleRevisionID, bindings.RuleFingerprint, bindings.RuleAcceptanceSHA = ruleOutput.RevisionID, ruleOutput.Fingerprint, ruleOutput.AcceptanceSHA

	var torrentOutput struct {
		Prepared           bool   `json:"prepared"`
		Verified           bool   `json:"verified"`
		Status             string `json:"status"`
		Target             string `json:"target"`
		TorrentArtifactID  string `json:"target_torrent_artifact_id"`
		TorrentStoragePath string `json:"target_torrent_storage_path"`
		TorrentSHA256      string `json:"target_torrent_sha256"`
		TorrentSizeBytes   int64  `json:"target_torrent_size_bytes"`
		ContentFingerprint string `json:"content_fingerprint_sha256"`
		ReceiptArtifactID  string `json:"receipt_artifact_id"`
		ReceiptSHA256      string `json:"receipt_sha256"`
		ReceiptStoragePath string `json:"receipt_storage_path"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_torrent", &torrentOutput) || !torrentOutput.Prepared || !torrentOutput.Verified ||
		torrentOutput.Status != "ready_for_upload" || torrentOutput.Target != bindings.Target || torrentOutput.TorrentArtifactID == "" ||
		torrentOutput.TorrentStoragePath == "" || torrentOutput.TorrentSHA256 == "" || torrentOutput.ReceiptArtifactID == "" ||
		torrentOutput.ReceiptSHA256 == "" || torrentOutput.ReceiptStoragePath == "" {
		return targetUploadBindings{}, fmt.Errorf("verified target_torrent evidence is missing")
	}
	bindings.TargetTorrentArtifact = sites.TargetArtifactEvidence{
		ArtifactID: torrentOutput.TorrentArtifactID, StoragePath: torrentOutput.TorrentStoragePath,
		SHA256: torrentOutput.TorrentSHA256, SizeBytes: torrentOutput.TorrentSizeBytes,
	}
	bindings.TargetTorrent, err = readTargetArtifact(executor.artifacts, bindings.TargetTorrentArtifact, maxTargetTorrentBytes)
	if err != nil {
		return targetUploadBindings{}, fmt.Errorf("target torrent artifact verification failed")
	}
	bindings.TorrentInspection, err = torrentmeta.Inspect(bindings.TargetTorrent)
	if err != nil || bindings.TorrentInspection.ContentFingerprint != torrentOutput.ContentFingerprint {
		return targetUploadBindings{}, fmt.Errorf("target torrent structure or fingerprint verification failed")
	}
	bindings.TargetTorrentReceipt = sites.TargetArtifactEvidence{
		ArtifactID: torrentOutput.ReceiptArtifactID, StoragePath: torrentOutput.ReceiptStoragePath, SHA256: torrentOutput.ReceiptSHA256,
	}
	receiptBody, err := readTargetArtifact(executor.artifacts, bindings.TargetTorrentReceipt, maxTargetPackageArtifact)
	var torrentReceipt targetTorrentReceipt
	if err != nil || json.Unmarshal(receiptBody, &torrentReceipt) != nil || torrentReceipt.SchemaVersion != 1 ||
		torrentReceipt.Target != bindings.Target || torrentReceipt.Artifact.SHA256 != bindings.TargetTorrentArtifact.SHA256 ||
		torrentReceipt.Bindings.TargetPackageSHA256 != bindings.PackageArtifact.SHA256 ||
		torrentReceipt.Bindings.DuplicateCheckSHA != bindings.PriorDuplicate.SHA256 ||
		torrentReceipt.Bindings.RuleRevisionID != bindings.RuleRevisionID || torrentReceipt.Bindings.RuleFingerprint != bindings.RuleFingerprint ||
		torrentReceipt.Bindings.RuleAcceptanceSHA != bindings.RuleAcceptanceSHA {
		return targetUploadBindings{}, fmt.Errorf("target torrent receipt verification failed")
	}
	return bindings, nil
}

func (executor targetUploadExecutor) currentRuleBlock(ctx context.Context, bindings targetUploadBindings) *BlockError {
	revision, err := executor.rules.Active(ctx, bindings.Target)
	if errors.Is(err, rules.ErrNotFound) {
		return targetRuleChangedBlock(bindings, "the target site has no active approved rule revision")
	}
	if err != nil {
		return &BlockError{
			Blockers:    []Blocker{{Code: "target_rule_check_failed", Message: "the current target rule revision could not be loaded", SiteCode: bindings.Target}},
			NextActions: []NextAction{{Action: "retry_current_rule_check", Description: "Restore rule-store availability before attempting any upload."}},
			ResumeState: map[string]any{"confirm_upload": true},
		}
	}
	if revision.Status != "approved" || revision.ID != bindings.RuleRevisionID || revision.Fingerprint != bindings.RuleFingerprint {
		return targetRuleChangedBlock(bindings, "the active target rule revision changed after the target torrent was prepared")
	}
	policy, err := rules.ParsePolicy(revision.Policy)
	if err != nil || !policy.Source.Complete || !slices.Contains(policy.Site.Roles, "target") ||
		!policy.Automation.Retorrent || !policy.Automation.Upload || !policy.Automation.AutoUpload {
		return targetRuleChangedBlock(bindings, "the current target rule policy no longer permits this automated upload")
	}
	return nil
}

func (executor targetUploadExecutor) persistPreuploadDuplicate(ctx context.Context, execution Execution, bindings targetUploadBindings, evidence sites.TargetDuplicateEvidence) (sites.TargetArtifactEvidence, error) {
	document := preuploadDuplicateDocument{
		SchemaVersion: 1, TargetPackageSHA256: bindings.PackageArtifact.SHA256,
		TargetTorrentSHA256:  bindings.TargetTorrentArtifact.SHA256,
		PriorDuplicateSHA256: bindings.PriorDuplicate.SHA256, RuleFingerprint: bindings.RuleFingerprint,
		Evidence: evidence,
	}
	body, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("serialize pre-upload duplicate evidence: %w", err)
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(bindings.Target)+"-preupload-duplicate-check.json", bytes.NewReader(body))
	if err != nil {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("persist pre-upload duplicate evidence: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "preupload_duplicate_check", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": bindings.Target, "imdb_id": evidence.Query.IMDbID, "duplicate": evidence.Duplicate,
			"result_count": evidence.ResultCount, "target_torrent_sha256": bindings.TargetTorrentArtifact.SHA256,
			"configuration_sha256": evidence.ConfigurationSHA256, "rule_fingerprint": bindings.RuleFingerprint,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("register pre-upload duplicate evidence: %w", err)
	}
	return sites.TargetArtifactEvidence{
		ArtifactID: recorded.ID, StoragePath: recorded.StoragePath, SHA256: recorded.SHA256, SizeBytes: recorded.SizeBytes,
	}, nil
}

func validateTargetUploadEvidence(evidence sites.TargetUploadEvidence, bindings targetUploadBindings) error {
	if evidence.SiteCode != bindings.Target || evidence.Adapter != bindings.Package.Adapter || len(evidence.ConfigurationSHA256) != 64 ||
		evidence.TorrentID == "" || evidence.DetailsURL == "" || len(evidence.ResponseSHA256) != 64 || evidence.SubmittedAt.IsZero() {
		return fmt.Errorf("target upload result is incomplete or bound to another adapter")
	}
	for _, character := range evidence.TorrentID {
		if character < '0' || character > '9' {
			return fmt.Errorf("target upload torrent id is invalid")
		}
	}
	if evidence.DetailsURL != "https://kp.m-team.cc/details/"+evidence.TorrentID {
		return fmt.Errorf("target upload details URL is not bound to the returned torrent id")
	}
	return nil
}

func targetRuleChangedBlock(bindings targetUploadBindings, message string) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: "target_rule_changed", Message: message, SiteCode: bindings.Target}},
		NextActions: []NextAction{{
			Action: "review_current_target_rules", Description: "Review and accept the current active revision, then create a new job so all downstream artifacts bind to it.",
			Parameters: map[string]any{"site_code": bindings.Target, "previous_fingerprint": bindings.RuleFingerprint},
		}},
		ResumeState: map[string]any{"confirm_upload": false},
	}
}

func targetUploadAdapterBlock(err error, bindings targetUploadBindings, freshDuplicate sites.TargetArtifactEvidence) *BlockError {
	code, message, _ := sites.ErrorDetails(err)
	action := "review_target_upload_failure"
	description := "Review the target response and immutable upload inputs before deciding whether to resume."
	switch code {
	case "target_upload_outcome_unknown":
		action = "reconcile_target_upload"
		description = "Do not retry blindly. Run a fresh duplicate search and inspect MTEAM for the submitted release or recover its torrent id."
	case "site_api_key_required", "site_authentication_failed", "site_configuration_unavailable", "site_configuration_invalid":
		action = "configure_target_site"
		description = "Configure or refresh the encrypted MTEAM API credential and endpoint before resuming."
	case "target_upload_adapter_unavailable", "site_adapter_mismatch":
		action = "configure_target_adapter"
		description = "Enable the reviewed target upload adapter before resuming."
	case "target_upload_request_invalid":
		action = "restart_from_verified_target_package"
		description = "Do not upload; rebuild the target package and torrent from verified immutable evidence."
	}
	return &BlockError{
		Blockers: []Blocker{{Code: code, Message: message, SiteCode: bindings.Target}},
		NextActions: []NextAction{{Action: action, Description: description, Parameters: map[string]any{
			"site_code": bindings.Target, "preupload_duplicate_check_sha256": freshDuplicate.SHA256,
		}}},
		ResumeState: map[string]any{"target_upload": map[string]any{
			"outcome": "unreconciled", "preupload_duplicate_check": freshDuplicate,
			"submitted_torrent_sha256": bindings.TargetTorrentArtifact.SHA256,
		}, "confirm_upload": false},
	}
}

func targetUploadEvidenceBlock(code, message string, bindings targetUploadBindings) *BlockError {
	return &BlockError{
		Blockers:    []Blocker{{Code: code, Message: message, SiteCode: bindings.Target}},
		NextActions: []NextAction{{Action: "restart_from_verified_target_package", Description: "Do not upload; rebuild the job from verified target evidence."}},
		ResumeState: map[string]any{"confirm_upload": false},
	}
}
