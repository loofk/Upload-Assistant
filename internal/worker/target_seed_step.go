package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func WithTargetSeedVerification(provider DownloaderProvider, artifactStore WorkflowArtifactStore) Option {
	return func(runner *Runner) {
		runner.executors["target_seed_verify"] = targetSeedVerifyExecutor{
			provider: provider, artifacts: artifactStore, recorder: runner.runtime, now: time.Now,
		}
	}
}

type targetSeedVerifyExecutor struct {
	provider  DownloaderProvider
	artifacts WorkflowArtifactStore
	recorder  ArtifactRecorder
	now       func() time.Time
}

type targetSeedBindings struct {
	Target           string
	TorrentID        string
	DownloaderName   string
	TorrentHash      string
	ConfigurationSHA string
	ExpectedPath     string
	ContentFileCount int
	ContentSizeBytes int64
	Options          targetInjectOptionsReceipt
	Rule             targetInjectRuleReceipt
	TorrentArtifact  sites.TargetArtifactEvidence
	InjectReceipt    sites.TargetArtifactEvidence
	InjectDocument   targetInjectReceipt
}

type targetSeedObservation struct {
	SchemaVersion     int                              `json:"schema_version"`
	Target            string                           `json:"target"`
	TorrentID         string                           `json:"torrent_id"`
	InjectionReceipt  sites.TargetArtifactEvidence     `json:"target_injection_receipt"`
	TargetTorrent     sites.TargetArtifactEvidence     `json:"target_torrent"`
	InjectedConfigSHA string                           `json:"injected_configuration_sha256"`
	ObservedConfigSHA string                           `json:"observed_configuration_sha256"`
	Torrent           downloaders.TorrentEvidence      `json:"torrent"`
	Files             downloaders.TorrentFilesEvidence `json:"files"`
	Requirements      rules.Seeding                    `json:"requirements"`
	Checks            targetSeedChecks                 `json:"checks"`
	ObservedAt        time.Time                        `json:"observed_at"`
}

type targetSeedChecks struct {
	DownloaderMatches      bool    `json:"downloader_matches"`
	HashMatches            bool    `json:"hash_matches"`
	Complete               bool    `json:"complete"`
	SeedingState           bool    `json:"seeding_state"`
	ContentPathMatches     bool    `json:"content_path_matches"`
	FileManifestMatches    bool    `json:"file_manifest_matches"`
	CategoryMatches        bool    `json:"category_matches"`
	TagsMatch              bool    `json:"tags_match"`
	DownloadLimitSafe      bool    `json:"download_limit_safe"`
	UploadLimitSafe        bool    `json:"upload_limit_safe"`
	ObservedSeedingSeconds int64   `json:"observed_seeding_seconds"`
	MinimumSeedingSeconds  int64   `json:"minimum_seeding_seconds"`
	ObservedRatio          float64 `json:"observed_ratio"`
	MinimumRatio           float64 `json:"minimum_ratio"`
	TimeRequirementMet     bool    `json:"time_requirement_met"`
	RatioRequirementMet    bool    `json:"ratio_requirement_met"`
}

func (executor targetSeedVerifyExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("target seed verification dependencies are unavailable")
	}
	bindings, err := executor.inputs(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	torrentEvidence, err := executor.provider.Inspect(ctx, bindings.DownloaderName, bindings.TorrentHash, execution.Actor)
	if err != nil {
		return nil, downloaderBlock(err, bindings.DownloaderName, "inspect_target_seed", map[string]any{
			"torrent_hash": bindings.TorrentHash, "target": bindings.Target, "torrent_id": bindings.TorrentID,
		})
	}
	filesEvidence, err := executor.provider.Files(ctx, bindings.DownloaderName, bindings.TorrentHash, execution.Actor)
	if err != nil {
		return nil, downloaderBlock(err, bindings.DownloaderName, "inspect_target_seed_files", map[string]any{
			"torrent_hash": bindings.TorrentHash, "target": bindings.Target, "torrent_id": bindings.TorrentID,
		})
	}
	now := time.Now().UTC()
	if executor.now != nil {
		now = executor.now().UTC()
	}
	checks := evaluateTargetSeed(bindings, torrentEvidence, filesEvidence)
	observation := targetSeedObservation{
		SchemaVersion: 1, Target: bindings.Target, TorrentID: bindings.TorrentID,
		InjectionReceipt: bindings.InjectReceipt, TargetTorrent: bindings.TorrentArtifact,
		InjectedConfigSHA: bindings.ConfigurationSHA, ObservedConfigSHA: torrentEvidence.ConfigurationSHA256,
		Torrent: torrentEvidence, Files: filesEvidence, Requirements: bindings.Rule.Seeding,
		Checks: checks, ObservedAt: now,
	}
	artifact, err := executor.persistObservation(ctx, execution, observation)
	if err != nil {
		return nil, err
	}
	if blocker := targetSeedBlock(bindings, checks, artifact); blocker != nil {
		return nil, blocker
	}
	return mustJSON(map[string]any{
		"verified": true, "status": "seeding_requirements_satisfied", "target": bindings.Target,
		"uploaded_torrent_id": bindings.TorrentID, "downloader_name": bindings.DownloaderName,
		"torrent_hash": bindings.TorrentHash, "torrent": torrentEvidence, "files": filesEvidence,
		"requirements": bindings.Rule.Seeding, "checks": checks,
		"target_injection_receipt_sha256": bindings.InjectReceipt.SHA256,
		"observation_artifact_id":         artifact.ArtifactID, "observation_sha256": artifact.SHA256,
		"observation_storage_path": artifact.StoragePath,
	}), nil
}

func (executor targetSeedVerifyExecutor) inputs(snapshotBody json.RawMessage) (targetSeedBindings, error) {
	var snapshot struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return targetSeedBindings{}, fmt.Errorf("decode target seed snapshot: %w", err)
	}
	var injected struct {
		Injected          bool                       `json:"injected"`
		Status            string                     `json:"status"`
		Target            string                     `json:"target"`
		TorrentID         string                     `json:"uploaded_torrent_id"`
		DownloaderName    string                     `json:"downloader_name"`
		ConfigurationSHA  string                     `json:"downloader_configuration_sha256"`
		TorrentHash       string                     `json:"torrent_hash"`
		Options           targetInjectOptionsReceipt `json:"options"`
		Rule              targetInjectRuleReceipt    `json:"rule"`
		ExpectedPath      string                     `json:"expected_remote_content_path"`
		TorrentArtifactID string                     `json:"target_torrent_artifact_id"`
		TorrentSHA256     string                     `json:"target_torrent_sha256"`
		ReceiptArtifactID string                     `json:"receipt_artifact_id"`
		ReceiptSHA256     string                     `json:"receipt_sha256"`
		ReceiptPath       string                     `json:"receipt_storage_path"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_inject", &injected) || !injected.Injected || injected.Status != "injected" ||
		injected.Target == "" || injected.TorrentID == "" || injected.DownloaderName == "" || len(injected.ConfigurationSHA) != 64 ||
		injected.TorrentHash == "" || injected.ExpectedPath == "" || injected.TorrentArtifactID == "" || injected.TorrentSHA256 == "" ||
		injected.ReceiptArtifactID == "" || injected.ReceiptSHA256 == "" || injected.ReceiptPath == "" || len(injected.Rule.Fingerprint) != 64 {
		return targetSeedBindings{}, fmt.Errorf("completed target_inject evidence is missing")
	}
	bindings := targetSeedBindings{
		Target: injected.Target, TorrentID: injected.TorrentID, DownloaderName: injected.DownloaderName,
		TorrentHash: injected.TorrentHash, ConfigurationSHA: injected.ConfigurationSHA, ExpectedPath: injected.ExpectedPath,
		Options: injected.Options, Rule: injected.Rule,
		TorrentArtifact: sites.TargetArtifactEvidence{ArtifactID: injected.TorrentArtifactID, SHA256: injected.TorrentSHA256},
		InjectReceipt: sites.TargetArtifactEvidence{
			ArtifactID: injected.ReceiptArtifactID, StoragePath: injected.ReceiptPath, SHA256: injected.ReceiptSHA256,
		},
	}
	receiptBody, err := readTargetArtifact(executor.artifacts, bindings.InjectReceipt, maxTargetPackageArtifact)
	if err != nil || json.Unmarshal(receiptBody, &bindings.InjectDocument) != nil || bindings.InjectDocument.SchemaVersion != 1 ||
		bindings.InjectDocument.Target != bindings.Target || bindings.InjectDocument.TorrentID != bindings.TorrentID ||
		bindings.InjectDocument.TargetTorrent.SHA256 != bindings.TorrentArtifact.SHA256 ||
		bindings.InjectDocument.Add.DownloaderName != bindings.DownloaderName ||
		bindings.InjectDocument.Add.ConfigurationSHA256 != bindings.ConfigurationSHA ||
		!hashMatches(bindings.TorrentHash, bindings.InjectDocument.Add.Result.Hashes) ||
		!equalTargetInjectOptions(bindings.InjectDocument.Options, bindings.Options) || bindings.InjectDocument.Rule.Fingerprint != bindings.Rule.Fingerprint {
		return targetSeedBindings{}, fmt.Errorf("target injection receipt verification failed")
	}
	bindings.TorrentArtifact = bindings.InjectDocument.TargetTorrent
	var content struct {
		Resolved  bool  `json:"resolved"`
		FileCount int   `json:"file_count"`
		TotalSize int64 `json:"total_size_bytes"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "content_resolve", &content) || !content.Resolved || content.FileCount <= 0 || content.TotalSize <= 0 {
		return targetSeedBindings{}, fmt.Errorf("verified content manifest evidence is missing")
	}
	bindings.ContentFileCount, bindings.ContentSizeBytes = content.FileCount, content.TotalSize
	return bindings, nil
}

func equalTargetInjectOptions(left, right targetInjectOptionsReceipt) bool {
	return left.DownloaderName == right.DownloaderName && left.SavePath == right.SavePath && left.Category == right.Category &&
		slices.Equal(left.Tags, right.Tags) && left.DownloadLimit == right.DownloadLimit && left.UploadLimit == right.UploadLimit &&
		left.SkipChecking == right.SkipChecking && left.Paused == right.Paused
}

func evaluateTargetSeed(bindings targetSeedBindings, torrent downloaders.TorrentEvidence, files downloaders.TorrentFilesEvidence) targetSeedChecks {
	state := strings.ToLower(strings.TrimSpace(torrent.Torrent.State))
	downloaderMatches := torrent.DownloaderName == bindings.DownloaderName && torrent.Adapter == "qbittorrent" &&
		len(torrent.ConfigurationSHA256) == 64
	seedingState := !strings.Contains(state, "paused") && !strings.Contains(state, "stopped") &&
		!strings.Contains(state, "error") && !strings.Contains(state, "missing")
	complete := torrent.Torrent.Progress >= 0.999999 || torrent.Torrent.TotalSize > 0 && torrent.Torrent.AmountLeft == 0
	filesMatch := files.DownloaderName == bindings.DownloaderName && files.Adapter == "qbittorrent" &&
		files.Torrent.DownloaderName == bindings.DownloaderName && files.Torrent.Adapter == "qbittorrent" &&
		len(files.Torrent.ConfigurationSHA256) == 64 && files.Torrent.RemoteContentPath == bindings.ExpectedPath &&
		hashMatches(files.Torrent.Torrent.Hash, bindings.InjectDocument.Add.Result.Hashes) &&
		files.FileCount == bindings.ContentFileCount && files.TotalSize == bindings.ContentSizeBytes && len(files.Files) == bindings.ContentFileCount
	if filesMatch {
		for _, file := range files.Files {
			if file.Progress < 0.999999 || file.Size < 0 {
				filesMatch = false
				break
			}
		}
	}
	minimumSeconds := int64(bindings.Rule.Seeding.MinimumTimeHours) * 3600
	seedingSeconds := torrent.Torrent.SeedingTime
	if seedingSeconds < 0 {
		seedingSeconds = 0
	}
	return targetSeedChecks{
		DownloaderMatches: downloaderMatches,
		HashMatches:       hashMatches(torrent.Torrent.Hash, bindings.InjectDocument.Add.Result.Hashes), Complete: complete,
		SeedingState: seedingState, ContentPathMatches: torrent.RemoteContentPath == bindings.ExpectedPath &&
			torrent.RemoteSavePath == bindings.Options.SavePath && torrent.Torrent.TotalSize == bindings.ContentSizeBytes,
		FileManifestMatches: filesMatch, CategoryMatches: torrent.Torrent.Category == bindings.Options.Category,
		TagsMatch:              tagsContainAll(torrent.Torrent.Tags, bindings.Options.Tags),
		DownloadLimitSafe:      limitWithinCap(torrent.Torrent.DownloadLimit, bindings.Options.DownloadLimit),
		UploadLimitSafe:        limitWithinCap(torrent.Torrent.UploadLimit, bindings.Options.UploadLimit),
		ObservedSeedingSeconds: seedingSeconds, MinimumSeedingSeconds: minimumSeconds,
		ObservedRatio: torrent.Torrent.Ratio, MinimumRatio: bindings.Rule.Seeding.MinimumRatio,
		TimeRequirementMet:  seedingSeconds >= minimumSeconds,
		RatioRequirementMet: torrent.Torrent.Ratio >= bindings.Rule.Seeding.MinimumRatio,
	}
}

func limitWithinCap(observed, cap int64) bool {
	if cap == 0 {
		return observed <= 0
	}
	return observed > 0 && observed <= cap
}

func (executor targetSeedVerifyExecutor) persistObservation(ctx context.Context, execution Execution, observation targetSeedObservation) (sites.TargetArtifactEvidence, error) {
	body, err := json.MarshalIndent(observation, "", "  ")
	if err != nil {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("serialize target seed observation: %w", err)
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(observation.Target)+"-target-seed-observation.json", bytes.NewReader(body))
	if err != nil {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("persist target seed observation: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "target_seed_observation", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": observation.Target, "torrent_id": observation.TorrentID,
			"downloader_name": observation.Torrent.DownloaderName, "torrent_hash": observation.Torrent.Torrent.Hash,
			"state": observation.Torrent.Torrent.State, "progress": observation.Torrent.Torrent.Progress,
			"ratio": observation.Checks.ObservedRatio, "seeding_seconds": observation.Checks.ObservedSeedingSeconds,
			"target_injection_receipt_sha256": observation.InjectionReceipt.SHA256,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return sites.TargetArtifactEvidence{}, fmt.Errorf("register target seed observation: %w", err)
	}
	return sites.TargetArtifactEvidence{
		ArtifactID: recorded.ID, StoragePath: recorded.StoragePath, SHA256: recorded.SHA256, SizeBytes: recorded.SizeBytes,
	}, nil
}

func targetSeedBlock(bindings targetSeedBindings, checks targetSeedChecks, artifact sites.TargetArtifactEvidence) *BlockError {
	resume := map[string]any{"target_seed_verify": map[string]any{
		"downloader_name": bindings.DownloaderName, "torrent_hash": bindings.TorrentHash,
		"observation_artifact_id": artifact.ArtifactID, "observation_sha256": artifact.SHA256,
	}}
	parameters := map[string]any{
		"downloader_name": bindings.DownloaderName, "torrent_hash": bindings.TorrentHash,
		"observation_artifact_id": artifact.ArtifactID,
	}
	if !checks.DownloaderMatches || !checks.HashMatches || !checks.ContentPathMatches || !checks.FileManifestMatches {
		return &BlockError{
			Blockers:    []Blocker{{Code: "target_seed_content_mismatch", Message: "qBittorrent target torrent content/path evidence does not match the immutable upload payload", SiteCode: bindings.Target}},
			NextActions: []NextAction{{Action: "repair_target_cross_seed_path", Description: "Stop and repair the target torrent save path or payload before seeding.", Parameters: parameters}},
			ResumeState: resume,
		}
	}
	if !checks.CategoryMatches || !checks.TagsMatch || !checks.DownloadLimitSafe || !checks.UploadLimitSafe {
		return &BlockError{
			Blockers:    []Blocker{{Code: "target_seed_policy_mismatch", Message: "qBittorrent category, tags, or rate limits do not match the audited target injection policy", SiteCode: bindings.Target}},
			NextActions: []NextAction{{Action: "repair_target_downloader_policy", Description: "Apply the receipt-bound category, tags, and strict rate limits, then resume verification.", Parameters: parameters}},
			ResumeState: resume,
		}
	}
	if !checks.Complete || !checks.SeedingState {
		return &BlockError{
			Blockers:    []Blocker{{Code: "target_seed_verification_pending", Message: "qBittorrent is still checking the payload or is not in a seeding-capable state", SiteCode: bindings.Target}},
			NextActions: []NextAction{{Action: "resume_when_target_is_seeding", Description: "Keep the verified target torrent active, then resume this job after qBittorrent finishes checking.", Parameters: parameters}},
			ResumeState: resume,
		}
	}
	if !checks.TimeRequirementMet || !checks.RatioRequirementMet {
		parameters["minimum_seeding_seconds"] = checks.MinimumSeedingSeconds
		parameters["observed_seeding_seconds"] = checks.ObservedSeedingSeconds
		parameters["minimum_ratio"] = checks.MinimumRatio
		parameters["observed_ratio"] = checks.ObservedRatio
		return &BlockError{
			Blockers:    []Blocker{{Code: "target_seeding_obligation_pending", Message: "the target rule's minimum seeding time or ratio has not yet been satisfied", SiteCode: bindings.Target}},
			NextActions: []NextAction{{Action: "continue_target_seeding", Description: "Keep the torrent seeding under the audited rate limits and resume this job later.", Parameters: parameters}},
			ResumeState: resume,
		}
	}
	return nil
}
