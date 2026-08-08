package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type TargetTorrentDownloader interface {
	DownloadUploadedTorrent(context.Context, string, sites.TargetTorrentDownloadRequest, workflow.Actor) (sites.DownloadedTargetTorrent, error)
}

func WithTargetTorrentDownloads(downloader TargetTorrentDownloader, artifactStore WorkflowArtifactStore) Option {
	return func(runner *Runner) {
		runner.executors["target_torrent_download"] = targetTorrentDownloadExecutor{
			downloader: downloader, artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type targetTorrentDownloadExecutor struct {
	downloader TargetTorrentDownloader
	artifacts  WorkflowArtifactStore
	recorder   ArtifactRecorder
}

type targetTorrentDownloadBindings struct {
	Target                   string
	TorrentID                string
	SubmittedTorrent         []byte
	SubmittedInspection      torrentmeta.Inspection
	SubmittedTorrentArtifact sites.TargetArtifactEvidence
	UploadReceiptArtifact    sites.TargetArtifactEvidence
	UploadReceipt            targetUploadReceipt
}

type targetTorrentDownloadReceipt struct {
	SchemaVersion    int                                 `json:"schema_version"`
	Target           string                              `json:"target"`
	TorrentID        string                              `json:"torrent_id"`
	UploadReceipt    sites.TargetArtifactEvidence        `json:"upload_receipt"`
	SubmittedTorrent sites.TargetArtifactEvidence        `json:"submitted_torrent"`
	Download         sites.TargetTorrentDownloadEvidence `json:"download"`
	Artifact         sites.TargetArtifactEvidence        `json:"artifact"`
	VerifiedAt       time.Time                           `json:"verified_at"`
}

func (executor targetTorrentDownloadExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.downloader == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("target torrent download workflow dependencies are unavailable")
	}
	bindings, err := executor.inputs(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	downloaded, err := executor.downloader.DownloadUploadedTorrent(ctx, bindings.Target, sites.TargetTorrentDownloadRequest{
		JobID: execution.Job.ID, AttemptID: execution.Attempt.ID, TorrentID: bindings.TorrentID,
		UploadReceiptSHA256: bindings.UploadReceiptArtifact.SHA256, SubmittedTorrentSHA256: bindings.SubmittedTorrentArtifact.SHA256,
		ContentFingerprintSHA256: bindings.SubmittedInspection.ContentFingerprint,
	}, execution.Actor)
	if err != nil {
		return nil, targetTorrentDownloadBlock(err, bindings)
	}
	inspection, err := validateDownloadedTargetTorrent(downloaded, bindings)
	if err != nil {
		return nil, targetTorrentDownloadEvidenceBlock("target_torrent_download_evidence_invalid", err.Error(), bindings)
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(bindings.Target)+"-"+bindings.TorrentID+"-downloaded.torrent", bytes.NewReader(downloaded.Bytes))
	if err != nil {
		return nil, fmt.Errorf("persist downloaded target torrent: %w", err)
	}
	torrentArtifact, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "target_downloaded_torrent", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/x-bittorrent", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": bindings.Target, "torrent_id": bindings.TorrentID,
			"v1_infohash": inspection.Hashes.V1SHA1, "v2_infohash": inspection.Hashes.V2SHA256,
			"content_fingerprint_sha256": inspection.ContentFingerprint, "announce_sha256": downloaded.Evidence.AnnounceSHA256,
			"submitted_torrent_sha256": bindings.SubmittedTorrentArtifact.SHA256,
			"upload_receipt_sha256":    bindings.UploadReceiptArtifact.SHA256,
			"configuration_sha256":     downloaded.Evidence.ConfigurationSHA256,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register downloaded target torrent: %w", err)
	}
	receipt := targetTorrentDownloadReceipt{
		SchemaVersion: 1, Target: bindings.Target, TorrentID: bindings.TorrentID,
		UploadReceipt: bindings.UploadReceiptArtifact, SubmittedTorrent: bindings.SubmittedTorrentArtifact,
		Download: downloaded.Evidence,
		Artifact: sites.TargetArtifactEvidence{
			ArtifactID: torrentArtifact.ID, StoragePath: torrentArtifact.StoragePath,
			SHA256: torrentArtifact.SHA256, SizeBytes: torrentArtifact.SizeBytes,
		},
		VerifiedAt: time.Now().UTC(),
	}
	receiptBody, err := json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize target torrent download receipt: %w", err)
	}
	receiptFile, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(bindings.Target)+"-target-torrent-download-receipt.json", bytes.NewReader(receiptBody))
	if err != nil {
		return nil, fmt.Errorf("persist target torrent download receipt: %w", err)
	}
	receiptArtifact, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "target_torrent_download_receipt", StoragePath: receiptFile.RelativePath, Filename: receiptFile.Filename,
		MIMEType: "application/json", SizeBytes: receiptFile.SizeBytes, SHA256: receiptFile.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": bindings.Target, "torrent_id": bindings.TorrentID,
			"target_torrent_artifact_id": torrentArtifact.ID, "target_torrent_sha256": torrentArtifact.SHA256,
			"content_fingerprint_sha256": inspection.ContentFingerprint,
			"upload_receipt_sha256":      bindings.UploadReceiptArtifact.SHA256,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register target torrent download receipt: %w", err)
	}
	return mustJSON(map[string]any{
		"downloaded": true, "verified": true, "status": "ready_for_injection", "target": bindings.Target,
		"uploaded_torrent_id":        bindings.TorrentID,
		"target_torrent_artifact_id": torrentArtifact.ID, "target_torrent_storage_path": torrentArtifact.StoragePath,
		"target_torrent_sha256": torrentArtifact.SHA256, "target_torrent_size_bytes": torrentArtifact.SizeBytes,
		"target_torrent_hashes": inspection.Hashes, "content_fingerprint_sha256": inspection.ContentFingerprint,
		"announce_sha256": downloaded.Evidence.AnnounceSHA256, "configuration_sha256": downloaded.Evidence.ConfigurationSHA256,
		"upload_receipt_sha256": bindings.UploadReceiptArtifact.SHA256,
		"receipt_artifact_id":   receiptArtifact.ID, "receipt_sha256": receiptArtifact.SHA256,
		"receipt_storage_path": receiptArtifact.StoragePath,
	}), nil
}

func (executor targetTorrentDownloadExecutor) inputs(snapshotBody json.RawMessage) (targetTorrentDownloadBindings, error) {
	var snapshot struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return targetTorrentDownloadBindings{}, fmt.Errorf("decode target torrent download snapshot: %w", err)
	}
	var parsed struct {
		Target string `json:"target"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "source_parse", &parsed) || parsed.Target == "" {
		return targetTorrentDownloadBindings{}, fmt.Errorf("source_parse target evidence is missing")
	}
	bindings := targetTorrentDownloadBindings{Target: strings.ToUpper(strings.TrimSpace(parsed.Target))}
	var submitted struct {
		Prepared           bool                   `json:"prepared"`
		Verified           bool                   `json:"verified"`
		Status             string                 `json:"status"`
		Target             string                 `json:"target"`
		ArtifactID         string                 `json:"target_torrent_artifact_id"`
		StoragePath        string                 `json:"target_torrent_storage_path"`
		SHA256             string                 `json:"target_torrent_sha256"`
		SizeBytes          int64                  `json:"target_torrent_size_bytes"`
		Hashes             torrentmeta.InfoHashes `json:"target_torrent_hashes"`
		ContentFingerprint string                 `json:"content_fingerprint_sha256"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_torrent", &submitted) || !submitted.Prepared || !submitted.Verified ||
		submitted.Status != "ready_for_upload" || submitted.Target != bindings.Target || submitted.ArtifactID == "" || submitted.StoragePath == "" ||
		submitted.SHA256 == "" || submitted.ContentFingerprint == "" {
		return targetTorrentDownloadBindings{}, fmt.Errorf("verified submitted target torrent evidence is missing")
	}
	bindings.SubmittedTorrentArtifact = sites.TargetArtifactEvidence{
		ArtifactID: submitted.ArtifactID, StoragePath: submitted.StoragePath, SHA256: submitted.SHA256, SizeBytes: submitted.SizeBytes,
	}
	var err error
	bindings.SubmittedTorrent, err = readTargetArtifact(executor.artifacts, bindings.SubmittedTorrentArtifact, maxTargetTorrentBytes)
	if err != nil {
		return targetTorrentDownloadBindings{}, fmt.Errorf("submitted target torrent artifact verification failed")
	}
	bindings.SubmittedInspection, err = torrentmeta.Inspect(bindings.SubmittedTorrent)
	if err != nil || bindings.SubmittedInspection.ContentFingerprint != submitted.ContentFingerprint ||
		bindings.SubmittedInspection.Hashes != submitted.Hashes {
		return targetTorrentDownloadBindings{}, fmt.Errorf("submitted target torrent structure or hashes do not match evidence")
	}
	var uploaded struct {
		Uploaded          bool                   `json:"uploaded"`
		Status            string                 `json:"status"`
		Target            string                 `json:"target"`
		TorrentID         string                 `json:"uploaded_torrent_id"`
		DetailsURL        string                 `json:"details_url"`
		SubmittedSHA256   string                 `json:"submitted_torrent_sha256"`
		SubmittedHashes   torrentmeta.InfoHashes `json:"submitted_infohashes"`
		ReceiptArtifactID string                 `json:"upload_receipt_artifact_id"`
		ReceiptSHA256     string                 `json:"upload_receipt_sha256"`
		ReceiptPath       string                 `json:"upload_receipt_storage_path"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_upload", &uploaded) || !uploaded.Uploaded || uploaded.Status != "uploaded" ||
		uploaded.Target != bindings.Target || uploaded.TorrentID == "" || uploaded.DetailsURL != "https://kp.m-team.cc/details/"+uploaded.TorrentID ||
		uploaded.SubmittedSHA256 != bindings.SubmittedTorrentArtifact.SHA256 || uploaded.SubmittedHashes != bindings.SubmittedInspection.Hashes ||
		uploaded.ReceiptArtifactID == "" || uploaded.ReceiptSHA256 == "" || uploaded.ReceiptPath == "" {
		return targetTorrentDownloadBindings{}, fmt.Errorf("successful target upload evidence is missing or inconsistent")
	}
	bindings.TorrentID = uploaded.TorrentID
	bindings.UploadReceiptArtifact = sites.TargetArtifactEvidence{
		ArtifactID: uploaded.ReceiptArtifactID, StoragePath: uploaded.ReceiptPath, SHA256: uploaded.ReceiptSHA256,
	}
	receiptBody, err := readTargetArtifact(executor.artifacts, bindings.UploadReceiptArtifact, maxTargetPackageArtifact)
	if err != nil || json.Unmarshal(receiptBody, &bindings.UploadReceipt) != nil || bindings.UploadReceipt.SchemaVersion != 1 ||
		bindings.UploadReceipt.Target != bindings.Target || !bindings.UploadReceipt.Confirmation.Confirmed ||
		bindings.UploadReceipt.Torrent.SHA256 != bindings.SubmittedTorrentArtifact.SHA256 ||
		bindings.UploadReceipt.Upload.TorrentID != bindings.TorrentID || bindings.UploadReceipt.Upload.DetailsURL != uploaded.DetailsURL ||
		bindings.UploadReceipt.Upload.Adapter == "" || bindings.UploadReceipt.FreshDuplicate.SHA256 == "" ||
		bindings.UploadReceipt.CurrentRule.Fingerprint == "" {
		return targetTorrentDownloadBindings{}, fmt.Errorf("target upload receipt verification failed")
	}
	return bindings, nil
}

func validateDownloadedTargetTorrent(downloaded sites.DownloadedTargetTorrent, bindings targetTorrentDownloadBindings) (torrentmeta.Inspection, error) {
	evidence := downloaded.Evidence
	if len(downloaded.Bytes) == 0 || evidence.SiteCode != bindings.Target || evidence.Adapter != bindings.UploadReceipt.Upload.Adapter ||
		len(evidence.ConfigurationSHA256) != 64 || evidence.TorrentID != bindings.TorrentID || evidence.Filename == "" ||
		evidence.SizeBytes != int64(len(downloaded.Bytes)) || len(evidence.SHA256) != 64 || len(evidence.AnnounceSHA256) != 64 ||
		len(evidence.TokenResponseSHA256) != 64 || len(evidence.SignedDownloadURLSHA256) != 64 || evidence.DownloadedAt.IsZero() {
		return torrentmeta.Inspection{}, fmt.Errorf("target torrent download evidence is incomplete or bound to another upload")
	}
	digest := sha256.Sum256(downloaded.Bytes)
	if !strings.EqualFold(hex.EncodeToString(digest[:]), evidence.SHA256) {
		return torrentmeta.Inspection{}, fmt.Errorf("downloaded target torrent SHA-256 does not match evidence")
	}
	inspection, err := torrentmeta.Inspect(downloaded.Bytes)
	if err != nil || inspection.Hashes != evidence.Hashes || inspection.ContentFingerprint != evidence.ContentFingerprint ||
		inspection.ContentFingerprint != bindings.SubmittedInspection.ContentFingerprint || inspection.Hashes != bindings.SubmittedInspection.Hashes ||
		!inspection.PrivateSet || !inspection.Private || inspection.Source != "MTEAM" {
		return torrentmeta.Inspection{}, fmt.Errorf("downloaded target torrent structure, hashes, or payload do not match the submitted torrent")
	}
	announceDigest := sha256.Sum256([]byte(inspection.Announce))
	if hex.EncodeToString(announceDigest[:]) != evidence.AnnounceSHA256 {
		return torrentmeta.Inspection{}, fmt.Errorf("downloaded target torrent announce binding does not match evidence")
	}
	return inspection, nil
}

func targetTorrentDownloadBlock(err error, bindings targetTorrentDownloadBindings) *BlockError {
	code, message, temporary := sites.ErrorDetails(err)
	action := "review_target_torrent_download"
	description := "Review the tracker-issued torrent evidence and target adapter before resuming."
	if temporary {
		action = "retry_target_torrent_download"
		description = "Restore MTEAM connectivity, then safely retry downloading the already-uploaded torrent."
	}
	switch code {
	case "site_api_key_required", "site_authentication_failed", "site_configuration_unavailable", "site_configuration_invalid":
		action = "configure_target_site"
		description = "Configure or refresh the encrypted MTEAM API credential and endpoint before resuming."
	case "target_torrent_download_url_rejected":
		action = "review_target_download_hosts"
		description = "Review MTEAM download_hosts and the hashed token response; never follow an untrusted signed URL."
	case "target_torrent_payload_mismatch", "target_torrent_download_invalid":
		action = "reconcile_uploaded_torrent"
		description = "Stop before qBittorrent injection and reconcile the target torrent with the immutable submitted payload."
	case "target_torrent_download_adapter_unavailable", "site_adapter_mismatch":
		action = "configure_target_adapter"
		description = "Enable the reviewed target torrent download adapter before resuming."
	}
	return &BlockError{
		Blockers: []Blocker{{Code: code, Message: message, SiteCode: bindings.Target}},
		NextActions: []NextAction{{Action: action, Description: description, Parameters: map[string]any{
			"site_code": bindings.Target, "torrent_id": bindings.TorrentID,
			"upload_receipt_sha256": bindings.UploadReceiptArtifact.SHA256,
		}}},
		ResumeState: map[string]any{"target_torrent_download": map[string]any{
			"torrent_id": bindings.TorrentID, "upload_receipt_sha256": bindings.UploadReceiptArtifact.SHA256,
		}},
	}
}

func targetTorrentDownloadEvidenceBlock(code, message string, bindings targetTorrentDownloadBindings) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: code, Message: message, SiteCode: bindings.Target}},
		NextActions: []NextAction{{Action: "reconcile_uploaded_torrent", Description: "Stop before qBittorrent injection and reconcile the target torrent evidence.", Parameters: map[string]any{
			"site_code": bindings.Target, "torrent_id": bindings.TorrentID,
		}}},
		ResumeState: map[string]any{"target_torrent_download": map[string]any{
			"torrent_id": bindings.TorrentID, "upload_receipt_sha256": bindings.UploadReceiptArtifact.SHA256,
		}},
	}
}
