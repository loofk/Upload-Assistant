package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"path"
	"slices"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func WithTargetInjection(provider DownloaderProvider, artifactStore WorkflowArtifactStore) Option {
	return func(runner *Runner) {
		runner.executors["target_inject"] = targetInjectExecutor{
			provider: provider, artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type targetInjectExecutor struct {
	provider  DownloaderProvider
	artifacts WorkflowArtifactStore
	recorder  ArtifactRecorder
}

type targetInjectBindings struct {
	Target                  string
	TorrentID               string
	Torrent                 []byte
	TorrentInspection       torrentmeta.Inspection
	TorrentArtifact         sites.TargetArtifactEvidence
	DownloadReceiptArtifact sites.TargetArtifactEvidence
	DownloadReceipt         targetTorrentDownloadReceipt
	UploadReceiptSHA256     string
	SourceDownloader        string
	RemoteContentRoot       string
	ContentFileCount        int
	ContentSizeBytes        int64
	TargetRuleFingerprint   string
	TargetRuleLimits        rules.Limits
	TargetSeeding           rules.Seeding
	Control                 downloaderControl
	AppliedDownloadLimit    int64
	AppliedUploadLimit      int64
}

type targetInjectReceipt struct {
	SchemaVersion       int                          `json:"schema_version"`
	Target              string                       `json:"target"`
	TorrentID           string                       `json:"torrent_id"`
	TargetTorrent       sites.TargetArtifactEvidence `json:"target_torrent"`
	DownloadReceipt     sites.TargetArtifactEvidence `json:"target_torrent_download_receipt"`
	UploadReceiptSHA256 string                       `json:"upload_receipt_sha256"`
	Rule                targetInjectRuleReceipt      `json:"rule"`
	Options             targetInjectOptionsReceipt   `json:"options"`
	Add                 downloaders.AddEvidence      `json:"add"`
	InjectedAt          time.Time                    `json:"injected_at"`
}

type targetInjectRuleReceipt struct {
	Fingerprint string        `json:"fingerprint"`
	Limits      rules.Limits  `json:"limits"`
	Seeding     rules.Seeding `json:"seeding"`
}

type targetInjectOptionsReceipt struct {
	DownloaderName string   `json:"downloader_name"`
	SavePath       string   `json:"save_path"`
	Category       string   `json:"category"`
	Tags           []string `json:"tags"`
	DownloadLimit  int64    `json:"download_limit_bytes_per_second"`
	UploadLimit    int64    `json:"upload_limit_bytes_per_second"`
	SkipChecking   bool     `json:"skip_checking"`
	Paused         bool     `json:"paused"`
}

func (executor targetInjectExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("target injection workflow dependencies are unavailable")
	}
	bindings, err := executor.inputs(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	if err := validateDownloaderControl(bindings.Control); err != nil {
		return nil, targetDownloaderConfigurationBlock(err, bindings)
	}
	if boolValue(bindings.Control.SkipChecking) {
		return nil, targetDownloaderConfigurationBlock(fmt.Errorf("target_downloader.skip_checking must be false so the configured downloader verifies the existing payload"), bindings)
	}
	if boolValue(bindings.Control.Paused) {
		return nil, targetDownloaderConfigurationBlock(fmt.Errorf("target_downloader.paused must be false so required seeding can start"), bindings)
	}
	evidence, err := executor.provider.Add(ctx, bindings.Control.Name, bindings.Torrent, qbittorrent.AddOptions{
		SavePath: bindings.Control.SavePath, Category: bindings.Control.Category, Tags: append([]string(nil), bindings.Control.Tags...),
		SkipChecking: false, Paused: false, DownloadLimit: bindings.AppliedDownloadLimit, UploadLimit: bindings.AppliedUploadLimit,
	}, execution.Actor)
	if err != nil {
		return nil, downloaderBlock(err, bindings.Control.Name, "inject_target_torrent", map[string]any{
			"target": bindings.Target, "torrent_id": bindings.TorrentID,
			"target_torrent_sha256": bindings.TorrentArtifact.SHA256,
		})
	}
	if err := validateTargetInjectionEvidence(evidence, bindings); err != nil {
		return nil, targetInjectionEvidenceBlock(err, bindings)
	}
	torrentHash := bindings.TorrentInspection.Hashes.V1SHA1
	if torrentHash == "" {
		torrentHash = bindings.TorrentInspection.Hashes.V2SHA256
	}
	receipt := targetInjectReceipt{
		SchemaVersion: 1, Target: bindings.Target, TorrentID: bindings.TorrentID,
		TargetTorrent: bindings.TorrentArtifact, DownloadReceipt: bindings.DownloadReceiptArtifact,
		UploadReceiptSHA256: bindings.UploadReceiptSHA256,
		Rule: targetInjectRuleReceipt{
			Fingerprint: bindings.TargetRuleFingerprint, Limits: bindings.TargetRuleLimits, Seeding: bindings.TargetSeeding,
		},
		Options: targetInjectOptionsReceipt{
			DownloaderName: bindings.Control.Name, SavePath: bindings.Control.SavePath,
			Category: bindings.Control.Category, Tags: append([]string(nil), bindings.Control.Tags...),
			DownloadLimit: bindings.AppliedDownloadLimit, UploadLimit: bindings.AppliedUploadLimit,
			SkipChecking: false, Paused: false,
		},
		Add: evidence, InjectedAt: time.Now().UTC(),
	}
	body, err := json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize target injection receipt: %w", err)
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(bindings.Target)+"-target-injection-receipt.json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("persist target injection receipt: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "target_injection_receipt", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": bindings.Target, "torrent_id": bindings.TorrentID, "downloader_name": bindings.Control.Name,
			"downloader_configuration_sha256": evidence.ConfigurationSHA256, "torrent_hash": torrentHash,
			"target_torrent_sha256": bindings.TorrentArtifact.SHA256, "download_receipt_sha256": bindings.DownloadReceiptArtifact.SHA256,
			"rule_fingerprint": bindings.TargetRuleFingerprint, "upload_limit": bindings.AppliedUploadLimit,
			"download_limit": bindings.AppliedDownloadLimit,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register target injection receipt: %w", err)
	}
	return mustJSON(map[string]any{
		"injected": true, "status": "injected", "target": bindings.Target, "uploaded_torrent_id": bindings.TorrentID,
		"downloader_name": bindings.Control.Name, "downloader_configuration_sha256": evidence.ConfigurationSHA256,
		"torrent_hash": torrentHash, "add_evidence": evidence,
		"options": receipt.Options, "rule": receipt.Rule,
		"expected_remote_content_path":           bindings.RemoteContentRoot,
		"target_torrent_artifact_id":             bindings.TorrentArtifact.ArtifactID,
		"target_torrent_sha256":                  bindings.TorrentArtifact.SHA256,
		"target_torrent_download_receipt_sha256": bindings.DownloadReceiptArtifact.SHA256,
		"upload_receipt_sha256":                  bindings.UploadReceiptSHA256,
		"receipt_artifact_id":                    recorded.ID, "receipt_sha256": recorded.SHA256,
		"receipt_storage_path": recorded.StoragePath,
	}), nil
}

func (executor targetInjectExecutor) inputs(snapshotBody json.RawMessage) (targetInjectBindings, error) {
	var snapshot struct {
		JobInput      retorrentRuntimeControls   `json:"job_input"`
		ResumeState   retorrentRuntimeControls   `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return targetInjectBindings{}, fmt.Errorf("decode target injection snapshot: %w", err)
	}
	var parsed struct {
		Target string `json:"target"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "source_parse", &parsed) || parsed.Target == "" {
		return targetInjectBindings{}, fmt.Errorf("source_parse target evidence is missing")
	}
	bindings := targetInjectBindings{Target: strings.ToUpper(strings.TrimSpace(parsed.Target))}
	var downloaded struct {
		Downloaded        bool                   `json:"downloaded"`
		Verified          bool                   `json:"verified"`
		Status            string                 `json:"status"`
		Target            string                 `json:"target"`
		TorrentID         string                 `json:"uploaded_torrent_id"`
		ArtifactID        string                 `json:"target_torrent_artifact_id"`
		StoragePath       string                 `json:"target_torrent_storage_path"`
		SHA256            string                 `json:"target_torrent_sha256"`
		SizeBytes         int64                  `json:"target_torrent_size_bytes"`
		Hashes            torrentmeta.InfoHashes `json:"target_torrent_hashes"`
		Fingerprint       string                 `json:"content_fingerprint_sha256"`
		UploadReceiptSHA  string                 `json:"upload_receipt_sha256"`
		ReceiptArtifactID string                 `json:"receipt_artifact_id"`
		ReceiptSHA256     string                 `json:"receipt_sha256"`
		ReceiptPath       string                 `json:"receipt_storage_path"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_torrent_download", &downloaded) || !downloaded.Downloaded || !downloaded.Verified ||
		downloaded.Status != "ready_for_injection" || downloaded.Target != bindings.Target || downloaded.TorrentID == "" ||
		downloaded.ArtifactID == "" || downloaded.StoragePath == "" || downloaded.SHA256 == "" || downloaded.Fingerprint == "" ||
		downloaded.UploadReceiptSHA == "" || downloaded.ReceiptArtifactID == "" || downloaded.ReceiptSHA256 == "" || downloaded.ReceiptPath == "" {
		return targetInjectBindings{}, fmt.Errorf("verified target_torrent_download evidence is missing")
	}
	bindings.TorrentID, bindings.UploadReceiptSHA256 = downloaded.TorrentID, downloaded.UploadReceiptSHA
	bindings.TorrentArtifact = sites.TargetArtifactEvidence{
		ArtifactID: downloaded.ArtifactID, StoragePath: downloaded.StoragePath, SHA256: downloaded.SHA256, SizeBytes: downloaded.SizeBytes,
	}
	bindings.DownloadReceiptArtifact = sites.TargetArtifactEvidence{
		ArtifactID: downloaded.ReceiptArtifactID, StoragePath: downloaded.ReceiptPath, SHA256: downloaded.ReceiptSHA256,
	}
	var err error
	bindings.Torrent, err = readTargetArtifact(executor.artifacts, bindings.TorrentArtifact, maxTargetTorrentBytes)
	if err != nil {
		return targetInjectBindings{}, fmt.Errorf("downloaded target torrent artifact verification failed")
	}
	bindings.TorrentInspection, err = torrentmeta.Inspect(bindings.Torrent)
	if err != nil || bindings.TorrentInspection.Hashes != downloaded.Hashes || bindings.TorrentInspection.ContentFingerprint != downloaded.Fingerprint {
		return targetInjectBindings{}, fmt.Errorf("downloaded target torrent structure or hashes do not match evidence")
	}
	receiptBody, err := readTargetArtifact(executor.artifacts, bindings.DownloadReceiptArtifact, maxTargetPackageArtifact)
	if err != nil || json.Unmarshal(receiptBody, &bindings.DownloadReceipt) != nil || bindings.DownloadReceipt.SchemaVersion != 1 ||
		bindings.DownloadReceipt.Target != bindings.Target || bindings.DownloadReceipt.TorrentID != bindings.TorrentID ||
		bindings.DownloadReceipt.Artifact.SHA256 != bindings.TorrentArtifact.SHA256 ||
		bindings.DownloadReceipt.UploadReceipt.SHA256 != bindings.UploadReceiptSHA256 ||
		bindings.DownloadReceipt.Download.SHA256 != bindings.TorrentArtifact.SHA256 ||
		bindings.DownloadReceipt.Download.Hashes != bindings.TorrentInspection.Hashes {
		return targetInjectBindings{}, fmt.Errorf("target torrent download receipt verification failed")
	}
	var content struct {
		Resolved       bool   `json:"resolved"`
		DownloaderName string `json:"downloader_name"`
		RemoteRoot     string `json:"remote_root"`
		FileCount      int    `json:"file_count"`
		TotalSize      int64  `json:"total_size_bytes"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "content_resolve", &content) || !content.Resolved || content.DownloaderName == "" ||
		content.RemoteRoot == "" || content.FileCount <= 0 || content.TotalSize <= 0 ||
		content.FileCount != bindings.TorrentInspection.FileCount || content.TotalSize != bindings.TorrentInspection.TotalSizeBytes {
		return targetInjectBindings{}, fmt.Errorf("verified source content path evidence is missing or does not match target torrent")
	}
	if !strings.HasPrefix(content.RemoteRoot, "/") || path.Clean(content.RemoteRoot) != content.RemoteRoot ||
		path.Base(content.RemoteRoot) != bindings.TorrentInspection.Name || path.Base(bindings.TorrentInspection.Name) != bindings.TorrentInspection.Name {
		return targetInjectBindings{}, fmt.Errorf("source remote content root cannot be safely converted into a downloader save path")
	}
	bindings.SourceDownloader, bindings.RemoteContentRoot = content.DownloaderName, content.RemoteRoot
	bindings.ContentFileCount, bindings.ContentSizeBytes = content.FileCount, content.TotalSize
	var targetRule struct {
		SiteCode    string        `json:"site_code"`
		Role        string        `json:"role"`
		Accepted    bool          `json:"accepted"`
		Fingerprint string        `json:"fingerprint"`
		Limits      rules.Limits  `json:"limits"`
		Seeding     rules.Seeding `json:"seeding"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_rules", &targetRule) || targetRule.SiteCode != bindings.Target ||
		targetRule.Role != "target" || !targetRule.Accepted || len(targetRule.Fingerprint) != 64 {
		return targetInjectBindings{}, fmt.Errorf("accepted target rule evidence is missing")
	}
	bindings.TargetRuleFingerprint, bindings.TargetRuleLimits, bindings.TargetSeeding = targetRule.Fingerprint, targetRule.Limits, targetRule.Seeding
	control := snapshot.JobInput.TargetDownloader
	mergeDownloaderControl(&control, snapshot.ResumeState.TargetDownloader)
	explicitSavePath := control.SavePath != ""
	if control.Name == "" {
		control.Name = content.DownloaderName
	}
	derivedSavePath := path.Dir(content.RemoteRoot)
	if control.SavePath == "" {
		if control.Name != content.DownloaderName {
			return targetInjectBindings{}, fmt.Errorf("a different target_downloader requires an explicit save_path containing the existing payload")
		}
		control.SavePath = derivedSavePath
	}
	if control.Name == content.DownloaderName && explicitSavePath && control.SavePath != derivedSavePath {
		return targetInjectBindings{}, fmt.Errorf("target_downloader.save_path must be %s for the verified source content on the same downloader", derivedSavePath)
	}
	if control.Category == "" {
		control.Category = strings.ToLower(bindings.Target)
	}
	if control.Tags == nil {
		control.Tags = []string{"retorrent", strings.ToLower(bindings.Target)}
	}
	if control.SkipChecking == nil {
		value := false
		control.SkipChecking = &value
	}
	if control.Paused == nil {
		value := false
		control.Paused = &value
	}
	bindings.Control = control
	policyDownload, err := rules.ParseByteRate(targetRule.Limits.Download)
	if err != nil {
		return targetInjectBindings{}, fmt.Errorf("parse target rule download limit: %w", err)
	}
	policyUpload, err := rules.ParseByteRate(targetRule.Limits.Upload)
	if err != nil {
		return targetInjectBindings{}, fmt.Errorf("parse target rule upload limit: %w", err)
	}
	bindings.AppliedDownloadLimit = strictestLimit(control.DownloadLimit, policyDownload)
	bindings.AppliedUploadLimit = strictestLimit(control.UploadLimit, policyUpload)
	return bindings, nil
}

func validateTargetInjectionEvidence(evidence downloaders.AddEvidence, bindings targetInjectBindings) error {
	if evidence.DownloaderName != bindings.Control.Name || evidence.Adapter != "qbittorrent" || len(evidence.ConfigurationSHA256) != 64 ||
		evidence.TorrentBytes != len(bindings.Torrent) || !strings.EqualFold(evidence.TorrentSHA256, bindings.TorrentArtifact.SHA256) ||
		evidence.Result.Hashes != bindings.TorrentInspection.Hashes {
		return fmt.Errorf("downloader add evidence is incomplete or does not match the downloaded target torrent")
	}
	if evidence.Observed != nil {
		observed := evidence.Observed
		if observed.DownloaderName != bindings.Control.Name || observed.Adapter != "qbittorrent" ||
			observed.ConfigurationSHA256 != evidence.ConfigurationSHA256 || !hashMatches(observed.Torrent.Hash, bindings.TorrentInspection.Hashes) ||
			(observed.Torrent.TotalSize > 0 && observed.Torrent.TotalSize != bindings.ContentSizeBytes) {
			return fmt.Errorf("the configured downloader observed torrent does not match the target injection")
		}
	}
	return nil
}

func hashMatches(value string, hashes torrentmeta.InfoHashes) bool {
	return strings.EqualFold(value, hashes.V1SHA1) || hashes.V2SHA256 != "" && strings.EqualFold(value, hashes.V2SHA256)
}

func targetDownloaderConfigurationBlock(err error, bindings targetInjectBindings) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: "target_downloader_configuration_invalid", Message: err.Error(), SiteCode: bindings.Target}},
		NextActions: []NextAction{{Action: "provide_target_downloader_parameters", Description: "Supply reviewed target_downloader settings that point to the already-verified payload.", Parameters: map[string]any{
			"target_downloader": map[string]any{"name": bindings.Control.Name, "save_path": bindings.Control.SavePath, "skip_checking": false, "paused": false},
		}}},
		ResumeState: map[string]any{"target_downloader": bindings.Control},
	}
}

func targetInjectionEvidenceBlock(err error, bindings targetInjectBindings) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: "target_injection_evidence_invalid", Message: err.Error(), SiteCode: bindings.Target}},
		NextActions: []NextAction{{Action: "inspect_target_torrent_in_downloader", Description: "Inspect the configured downloader for the exact target infohash before deciding whether a retry is safe.", Parameters: map[string]any{
			"downloader_name": bindings.Control.Name, "v1_infohash": bindings.TorrentInspection.Hashes.V1SHA1,
		}}},
		ResumeState: map[string]any{"target_inject": map[string]any{
			"downloader_name": bindings.Control.Name, "target_torrent_sha256": bindings.TorrentArtifact.SHA256,
		}},
	}
}

func tagsContainAll(value string, required []string) bool {
	observed := strings.Split(value, ",")
	for index := range observed {
		observed[index] = strings.TrimSpace(observed[index])
	}
	for _, tag := range required {
		if !slices.Contains(observed, tag) {
			return false
		}
	}
	return true
}
