package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmaker"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const maxTargetTorrentBytes = 32 << 20

type TargetTorrentProfileProvider interface {
	TorrentProfile(context.Context, string) (sites.TargetTorrentProfile, error)
}

type TargetTorrentMaker interface {
	SanitizeAndCheck(context.Context, torrentmaker.Request) (torrentmaker.Result, error)
}

func WithTargetTorrents(profiles TargetTorrentProfileProvider, maker TargetTorrentMaker, artifactStore WorkflowArtifactStore) Option {
	return func(runner *Runner) {
		runner.executors["target_torrent"] = targetTorrentExecutor{
			profiles: profiles, maker: maker, artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type targetTorrentExecutor struct {
	profiles  TargetTorrentProfileProvider
	maker     TargetTorrentMaker
	artifacts WorkflowArtifactStore
	recorder  ArtifactRecorder
}

type targetTorrentBindings struct {
	Target             string
	TargetAdapter      string
	SourceTorrent      sites.TargetArtifactEvidence
	ContentManifest    sites.TargetArtifactEvidence
	ContentLocalRoot   string
	ContentFileCount   int
	ContentSizeBytes   int64
	TargetPackage      sites.TargetArtifactEvidence
	DuplicateCheck     sites.TargetArtifactEvidence
	RuleRevisionID     string
	RuleFingerprint    string
	RuleAcceptanceSHA  string
	SourceTorrentBytes []byte
}

type targetTorrentReceipt struct {
	SchemaVersion int                          `json:"schema_version"`
	Target        string                       `json:"target"`
	Adapter       string                       `json:"adapter"`
	Profile       targetTorrentProfileReceipt  `json:"profile"`
	Tool          targetTorrentToolReceipt     `json:"tool"`
	Source        torrentInspectionReceipt     `json:"source_torrent"`
	TargetTorrent torrentInspectionReceipt     `json:"target_torrent"`
	Bindings      targetTorrentBindingReceipt  `json:"bindings"`
	Artifact      sites.TargetArtifactEvidence `json:"artifact"`
	VerifiedAt    time.Time                    `json:"verified_at"`
}

type targetTorrentProfileReceipt struct {
	SourceTag            string   `json:"source_tag"`
	AnnounceSHA256       string   `json:"announce_sha256"`
	RequiredTopLevelKeys []string `json:"required_top_level_keys"`
	ProfileSHA256        string   `json:"profile_sha256"`
}

type targetTorrentToolReceipt struct {
	Name             string `json:"name"`
	Version          string `json:"version"`
	Verification     string `json:"verification"`
	ModifyDurationMS int64  `json:"modify_duration_ms"`
	CheckDurationMS  int64  `json:"check_duration_ms"`
}

type torrentInspectionReceipt struct {
	Hashes             torrentmeta.InfoHashes `json:"hashes"`
	AnnounceSHA256     string                 `json:"announce_sha256,omitempty"`
	Name               string                 `json:"name"`
	Source             string                 `json:"source,omitempty"`
	Private            bool                   `json:"private"`
	PieceLength        int64                  `json:"piece_length"`
	PieceCount         int                    `json:"piece_count"`
	FileCount          int                    `json:"file_count"`
	TotalSizeBytes     int64                  `json:"total_size_bytes"`
	ContentFingerprint string                 `json:"content_fingerprint_sha256"`
	TopLevelKeys       []string               `json:"top_level_keys"`
	InfoKeys           []string               `json:"info_keys"`
}

type targetTorrentBindingReceipt struct {
	SourceTorrentSHA256 string `json:"source_torrent_sha256"`
	ContentManifestSHA  string `json:"content_manifest_sha256"`
	TargetPackageSHA256 string `json:"target_package_sha256"`
	DuplicateCheckSHA   string `json:"duplicate_check_sha256"`
	RuleRevisionID      string `json:"rule_revision_id"`
	RuleFingerprint     string `json:"rule_fingerprint"`
	RuleAcceptanceSHA   string `json:"rule_acceptance_sha256"`
}

func (executor targetTorrentExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.profiles == nil || executor.maker == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("target torrent workflow dependencies are unavailable")
	}
	bindings, err := executor.inputs(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	profile, err := executor.profiles.TorrentProfile(ctx, bindings.Target)
	if err != nil {
		return nil, targetTorrentProfileBlock(err, bindings.Target)
	}
	if profile.Adapter != bindings.TargetAdapter {
		return nil, targetTorrentEvidenceBlock("target_torrent_adapter_mismatch", "target torrent profile does not match the immutable target package adapter", bindings)
	}
	sourceInspection, err := torrentmeta.Inspect(bindings.SourceTorrentBytes)
	if err != nil {
		return nil, targetTorrentEvidenceBlock("source_torrent_structure_invalid", "the immutable source torrent is not a supported, auditable v1 metainfo document", bindings)
	}
	if sourceInspection.FileCount != bindings.ContentFileCount || sourceInspection.TotalSizeBytes != bindings.ContentSizeBytes {
		return nil, targetTorrentEvidenceBlock("source_torrent_content_mismatch", "source torrent file count or size does not match the verified content manifest", bindings)
	}
	result, err := executor.maker.SanitizeAndCheck(ctx, torrentmaker.Request{
		SourceTorrent: bindings.SourceTorrentBytes, ContentPath: bindings.ContentLocalRoot,
		AnnounceURL: profile.AnnounceURL, SourceTag: profile.SourceTag,
		TopLevelKeys: append([]string(nil), profile.RequiredTopLevelKeys...),
	})
	if err != nil {
		return nil, targetTorrentToolBlock(err, bindings)
	}
	targetInspection, err := torrentmeta.Inspect(result.Torrent)
	if err != nil {
		return nil, targetTorrentEvidenceBlock("target_torrent_structure_invalid", "mkbrr output is not a valid, auditable v1 torrent", bindings)
	}
	if err := validateSanitizedTorrent(sourceInspection, targetInspection, profile, bindings); err != nil {
		return nil, targetTorrentEvidenceBlock("target_torrent_profile_violation", err.Error(), bindings)
	}

	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(bindings.Target)+"-upload.torrent", bytes.NewReader(result.Torrent))
	if err != nil {
		return nil, fmt.Errorf("persist target torrent artifact: %w", err)
	}
	torrentArtifact, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "target_torrent", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/x-bittorrent", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": bindings.Target, "adapter": profile.Adapter, "tool": result.Tool, "tool_version": result.Version,
			"v1_infohash": targetInspection.Hashes.V1SHA1, "v2_infohash": targetInspection.Hashes.V2SHA256,
			"content_fingerprint_sha256": targetInspection.ContentFingerprint,
			"source_torrent_sha256":      bindings.SourceTorrent.SHA256, "content_manifest_sha256": bindings.ContentManifest.SHA256,
			"target_package_sha256": bindings.TargetPackage.SHA256, "duplicate_check_sha256": bindings.DuplicateCheck.SHA256,
			"rule_fingerprint": bindings.RuleFingerprint,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register target torrent artifact: %w", err)
	}
	profileReceipt, err := buildTargetTorrentProfileReceipt(profile)
	if err != nil {
		return nil, fmt.Errorf("build target torrent profile receipt: %w", err)
	}
	receipt := targetTorrentReceipt{
		SchemaVersion: 1, Target: bindings.Target, Adapter: profile.Adapter, Profile: profileReceipt,
		Tool: targetTorrentToolReceipt{
			Name: result.Tool, Version: result.Version, Verification: result.Verification,
			ModifyDurationMS: result.ModifyDurationMS, CheckDurationMS: result.CheckDurationMS,
		},
		Source:        safeTorrentInspection(sourceInspection),
		TargetTorrent: safeTorrentInspection(targetInspection),
		Bindings: targetTorrentBindingReceipt{
			SourceTorrentSHA256: bindings.SourceTorrent.SHA256, ContentManifestSHA: bindings.ContentManifest.SHA256,
			TargetPackageSHA256: bindings.TargetPackage.SHA256, DuplicateCheckSHA: bindings.DuplicateCheck.SHA256,
			RuleRevisionID: bindings.RuleRevisionID, RuleFingerprint: bindings.RuleFingerprint,
			RuleAcceptanceSHA: bindings.RuleAcceptanceSHA,
		},
		Artifact: sites.TargetArtifactEvidence{
			ArtifactID: torrentArtifact.ID, StoragePath: torrentArtifact.StoragePath,
			SHA256: torrentArtifact.SHA256, SizeBytes: torrentArtifact.SizeBytes,
		},
		VerifiedAt: time.Now().UTC(),
	}
	receiptBody, err := json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize target torrent receipt: %w", err)
	}
	receiptFile, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(bindings.Target)+"-target-torrent-receipt.json", bytes.NewReader(receiptBody))
	if err != nil {
		return nil, fmt.Errorf("persist target torrent receipt: %w", err)
	}
	receiptArtifact, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "target_torrent_receipt", StoragePath: receiptFile.RelativePath, Filename: receiptFile.Filename,
		MIMEType: "application/json", SizeBytes: receiptFile.SizeBytes, SHA256: receiptFile.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": bindings.Target, "target_torrent_artifact_id": torrentArtifact.ID,
			"target_torrent_sha256": torrentArtifact.SHA256, "content_fingerprint_sha256": targetInspection.ContentFingerprint,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register target torrent receipt: %w", err)
	}
	return mustJSON(map[string]any{
		"prepared": true, "verified": true, "status": "ready_for_upload", "target": bindings.Target,
		"tool": result.Tool, "tool_version": result.Version, "verification": result.Verification,
		"target_torrent_artifact_id": torrentArtifact.ID, "target_torrent_storage_path": torrentArtifact.StoragePath,
		"target_torrent_sha256": torrentArtifact.SHA256, "target_torrent_size_bytes": torrentArtifact.SizeBytes,
		"target_torrent_hashes": targetInspection.Hashes, "content_fingerprint_sha256": targetInspection.ContentFingerprint,
		"receipt_artifact_id": receiptArtifact.ID, "receipt_sha256": receiptArtifact.SHA256,
		"receipt_storage_path": receiptArtifact.StoragePath,
		"bindings":             receipt.Bindings,
	}), nil
}

func (executor targetTorrentExecutor) inputs(snapshotBody json.RawMessage) (targetTorrentBindings, error) {
	var snapshot struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return targetTorrentBindings{}, fmt.Errorf("decode target torrent snapshot: %w", err)
	}
	var parsed struct {
		Target string `json:"target"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "source_parse", &parsed) || parsed.Target == "" {
		return targetTorrentBindings{}, fmt.Errorf("source_parse target evidence is missing")
	}
	parsed.Target = strings.ToUpper(strings.TrimSpace(parsed.Target))
	var source struct {
		ArtifactID  string                 `json:"artifact_id"`
		StoragePath string                 `json:"storage_path"`
		SizeBytes   int64                  `json:"size_bytes"`
		SHA256      string                 `json:"sha256"`
		Hashes      torrentmeta.InfoHashes `json:"hashes"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "source_torrent", &source) || source.ArtifactID == "" || source.StoragePath == "" || source.SHA256 == "" {
		return targetTorrentBindings{}, fmt.Errorf("source_torrent artifact evidence is missing")
	}
	bindings := targetTorrentBindings{Target: parsed.Target, SourceTorrent: sites.TargetArtifactEvidence{
		ArtifactID: source.ArtifactID, StoragePath: source.StoragePath, SHA256: source.SHA256, SizeBytes: source.SizeBytes,
	}}
	sourceBody, err := readTargetArtifact(executor.artifacts, bindings.SourceTorrent, maxTargetTorrentBytes)
	if err != nil {
		return targetTorrentBindings{}, fmt.Errorf("source torrent artifact verification failed")
	}
	hashes, err := torrentmeta.Hashes(sourceBody)
	if err != nil || hashes != source.Hashes {
		return targetTorrentBindings{}, fmt.Errorf("source torrent infohash evidence is invalid")
	}
	bindings.SourceTorrentBytes = sourceBody

	var content struct {
		Resolved            bool   `json:"resolved"`
		LocalRoot           string `json:"local_root"`
		FileCount           int    `json:"file_count"`
		TotalSizeBytes      int64  `json:"total_size_bytes"`
		ManifestArtifactID  string `json:"manifest_artifact_id"`
		ManifestSHA256      string `json:"manifest_sha256"`
		ManifestStoragePath string `json:"manifest_storage_path"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "content_resolve", &content) || !content.Resolved || content.LocalRoot == "" ||
		content.FileCount < 1 || content.ManifestArtifactID == "" || content.ManifestSHA256 == "" || content.ManifestStoragePath == "" {
		return targetTorrentBindings{}, fmt.Errorf("content_resolve evidence is missing")
	}
	bindings.ContentLocalRoot, bindings.ContentFileCount, bindings.ContentSizeBytes = content.LocalRoot, content.FileCount, content.TotalSizeBytes
	bindings.ContentManifest = sites.TargetArtifactEvidence{
		ArtifactID: content.ManifestArtifactID, StoragePath: content.ManifestStoragePath, SHA256: content.ManifestSHA256,
	}
	manifestBody, err := readTargetArtifact(executor.artifacts, bindings.ContentManifest, maxManifestBytes)
	if err != nil {
		return targetTorrentBindings{}, fmt.Errorf("content manifest artifact verification failed")
	}
	var manifest contentManifest
	if json.Unmarshal(manifestBody, &manifest) != nil || manifest.SchemaVersion != 1 || manifest.LocalRoot != content.LocalRoot ||
		manifest.FileCount != content.FileCount || len(manifest.ResolvedFiles) != content.FileCount || manifest.TotalSizeBytes != content.TotalSizeBytes {
		return targetTorrentBindings{}, fmt.Errorf("content manifest artifact is invalid or mismatched")
	}

	var targetPackage struct {
		Prepared              bool   `json:"prepared"`
		Target                string `json:"target"`
		TargetRuleRevisionID  string `json:"target_rule_revision_id"`
		TargetRuleFingerprint string `json:"target_rule_fingerprint"`
		PackageArtifactID     string `json:"package_artifact_id"`
		PackageSHA256         string `json:"package_sha256"`
		PackageStoragePath    string `json:"package_storage_path"`
		PackageSizeBytes      int64  `json:"package_size_bytes"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_package", &targetPackage) || !targetPackage.Prepared || targetPackage.Target != parsed.Target ||
		targetPackage.TargetRuleRevisionID == "" || len(targetPackage.TargetRuleFingerprint) != 64 ||
		targetPackage.PackageArtifactID == "" || targetPackage.PackageSHA256 == "" || targetPackage.PackageStoragePath == "" {
		return targetTorrentBindings{}, fmt.Errorf("target_package evidence is missing")
	}
	bindings.TargetPackage = sites.TargetArtifactEvidence{
		ArtifactID: targetPackage.PackageArtifactID, StoragePath: targetPackage.PackageStoragePath,
		SHA256: targetPackage.PackageSHA256, SizeBytes: targetPackage.PackageSizeBytes,
	}
	packageBody, err := readTargetArtifact(executor.artifacts, bindings.TargetPackage, maxTargetPackageArtifact)
	if err != nil {
		return targetTorrentBindings{}, fmt.Errorf("target package artifact verification failed")
	}
	var prepared sites.PreparedTargetPackage
	if json.Unmarshal(packageBody, &prepared) != nil || prepared.SchemaVersion != 1 || prepared.Target != parsed.Target ||
		prepared.Adapter == "" || prepared.Content.ManifestSHA256 != content.ManifestSHA256 {
		return targetTorrentBindings{}, fmt.Errorf("target package artifact is invalid or mismatched")
	}
	targetRuleEvidence, validTargetRuleEvidence := prepared.Evidence["target_rule"].(map[string]any)
	if !validTargetRuleEvidence || targetRuleEvidence["revision_id"] != targetPackage.TargetRuleRevisionID ||
		targetRuleEvidence["fingerprint"] != targetPackage.TargetRuleFingerprint {
		return targetTorrentBindings{}, fmt.Errorf("target package rule evidence is invalid or mismatched")
	}
	bindings.TargetAdapter = prepared.Adapter

	var duplicate struct {
		Checked       bool   `json:"checked"`
		Status        string `json:"status"`
		Target        string `json:"target"`
		Duplicate     bool   `json:"duplicate"`
		ArtifactID    string `json:"duplicate_check_artifact_id"`
		SHA256        string `json:"duplicate_check_sha256"`
		StoragePath   string `json:"duplicate_check_storage_path"`
		PackageSHA256 string `json:"target_package_sha256"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_duplicate_check", &duplicate) || !duplicate.Checked || duplicate.Status != "clean" ||
		duplicate.Duplicate || duplicate.Target != parsed.Target || duplicate.PackageSHA256 != targetPackage.PackageSHA256 ||
		duplicate.ArtifactID == "" || duplicate.SHA256 == "" || duplicate.StoragePath == "" {
		return targetTorrentBindings{}, fmt.Errorf("clean target_duplicate_check evidence is missing")
	}
	bindings.DuplicateCheck = sites.TargetArtifactEvidence{ArtifactID: duplicate.ArtifactID, StoragePath: duplicate.StoragePath, SHA256: duplicate.SHA256}
	duplicateBody, err := readTargetArtifact(executor.artifacts, bindings.DuplicateCheck, maxTargetPackageArtifact)
	if err != nil {
		return targetTorrentBindings{}, fmt.Errorf("duplicate-check artifact verification failed")
	}
	var duplicateDocument duplicateCheckDocument
	if json.Unmarshal(duplicateBody, &duplicateDocument) != nil || duplicateDocument.SchemaVersion != 1 || duplicateDocument.Evidence.Duplicate ||
		duplicateDocument.Evidence.SiteCode != parsed.Target || duplicateDocument.Evidence.Adapter != prepared.Adapter ||
		duplicateDocument.TargetPackage.SHA256 != targetPackage.PackageSHA256 {
		return targetTorrentBindings{}, fmt.Errorf("duplicate-check artifact is invalid or mismatched")
	}

	var rule struct {
		SiteCode      string `json:"site_code"`
		Role          string `json:"role"`
		RevisionID    string `json:"rule_revision_id"`
		Fingerprint   string `json:"fingerprint"`
		Accepted      bool   `json:"accepted"`
		AcceptanceSHA string `json:"acceptance_sha256"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "target_rules", &rule) || rule.SiteCode != parsed.Target || rule.Role != "target" ||
		!rule.Accepted || len(rule.Fingerprint) != 64 || len(rule.AcceptanceSHA) != 64 || rule.RevisionID == "" {
		return targetTorrentBindings{}, fmt.Errorf("accepted target_rules evidence is missing")
	}
	if rule.RevisionID != targetPackage.TargetRuleRevisionID || !strings.EqualFold(rule.Fingerprint, targetPackage.TargetRuleFingerprint) {
		return targetTorrentBindings{}, fmt.Errorf("target package was prepared against a different active rule revision")
	}
	bindings.RuleRevisionID, bindings.RuleFingerprint, bindings.RuleAcceptanceSHA = rule.RevisionID, rule.Fingerprint, rule.AcceptanceSHA
	return bindings, nil
}

func validateSanitizedTorrent(source, target torrentmeta.Inspection, profile sites.TargetTorrentProfile, bindings targetTorrentBindings) error {
	if source.ContentFingerprint != target.ContentFingerprint {
		return fmt.Errorf("mkbrr changed payload names, paths, sizes, piece length, or piece hashes")
	}
	if target.Announce != profile.AnnounceURL || target.Source != profile.SourceTag || !target.PrivateSet || !target.Private {
		return fmt.Errorf("target torrent announce, source tag, or private flag does not match the target adapter profile")
	}
	if target.FileCount != bindings.ContentFileCount || target.TotalSizeBytes != bindings.ContentSizeBytes {
		return fmt.Errorf("target torrent file count or size does not match the verified content manifest")
	}
	required := append([]string(nil), profile.RequiredTopLevelKeys...)
	sort.Strings(required)
	if !slices.Equal(target.TopLevelKeys, required) || len(target.ExtraTopLevelKeys) > 0 || len(target.ExtraInfoKeys) > 0 {
		return fmt.Errorf("target torrent contains metadata fields outside the target adapter profile")
	}
	return nil
}

func safeTorrentInspection(inspection torrentmeta.Inspection) torrentInspectionReceipt {
	return torrentInspectionReceipt{
		Hashes: inspection.Hashes, AnnounceSHA256: sha256Hex([]byte(inspection.Announce)),
		Name: inspection.Name, Source: inspection.Source, Private: inspection.Private,
		PieceLength: inspection.PieceLength, PieceCount: inspection.PieceCount,
		FileCount: inspection.FileCount, TotalSizeBytes: inspection.TotalSizeBytes,
		ContentFingerprint: inspection.ContentFingerprint,
		TopLevelKeys:       append([]string(nil), inspection.TopLevelKeys...), InfoKeys: append([]string(nil), inspection.InfoKeys...),
	}
}

func buildTargetTorrentProfileReceipt(profile sites.TargetTorrentProfile) (targetTorrentProfileReceipt, error) {
	required := append([]string(nil), profile.RequiredTopLevelKeys...)
	sort.Strings(required)
	receipt := targetTorrentProfileReceipt{
		SourceTag: profile.SourceTag, AnnounceSHA256: sha256Hex([]byte(profile.AnnounceURL)), RequiredTopLevelKeys: required,
	}
	body, err := json.Marshal(receipt)
	if err != nil {
		return targetTorrentProfileReceipt{}, err
	}
	receipt.ProfileSHA256 = sha256Hex(body)
	return receipt, nil
}

func sha256Hex(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}

func targetTorrentProfileBlock(err error, target string) *BlockError {
	code, message, _ := sites.ErrorDetails(err)
	return &BlockError{
		Blockers: []Blocker{{Code: code, Message: message, SiteCode: target}},
		NextActions: []NextAction{{
			Action: "configure_target_torrent_adapter", Description: "Configure a reviewed target torrent profile before resuming.",
			Parameters: map[string]any{"site_code": target},
		}},
		ResumeState: map[string]any{"target_torrent": map[string]any{"site_code": target}},
	}
}

func targetTorrentToolBlock(err error, bindings targetTorrentBindings) *BlockError {
	code, message, retryable := torrentmaker.ErrorDetails(err)
	action := "retry_target_torrent"
	description := "Retry the independently resumable torrent sanitizing and piece-verification step."
	switch code {
	case "torrent_tool_unavailable", "torrent_tool_version_invalid":
		action = "install_mkbrr"
		description = "Install the pinned mkbrr binary or correct UA_MKBRR_BIN before resuming."
	case "target_torrent_content_mismatch":
		action = "recheck_source_content"
		description = "Force a source torrent recheck in the downloader and repair local content before resuming."
	case "target_torrent_input_invalid", "target_torrent_workspace_failed":
		action = "repair_target_torrent_environment"
		description = "Repair the mounted content path or writable private temporary directory before resuming."
	}
	return &BlockError{
		Blockers: []Blocker{{Code: code, Message: message, SiteCode: bindings.Target}},
		NextActions: []NextAction{{Action: action, Description: description, Parameters: map[string]any{
			"site_code": bindings.Target, "retryable": retryable,
		}}},
		ResumeState: map[string]any{"target_torrent": map[string]any{
			"site_code": bindings.Target, "source_torrent_sha256": bindings.SourceTorrent.SHA256,
			"content_manifest_sha256": bindings.ContentManifest.SHA256,
		}},
	}
}

func targetTorrentEvidenceBlock(code, message string, bindings targetTorrentBindings) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: code, Message: message, SiteCode: bindings.Target}},
		NextActions: []NextAction{{
			Action: "restart_from_verified_source", Description: "Do not upload this torrent; review immutable source/content evidence and rebuild the job from verified data.",
			Parameters: map[string]any{"site_code": bindings.Target},
		}},
		ResumeState: map[string]any{"target_torrent": map[string]any{
			"source_torrent_sha256":   bindings.SourceTorrent.SHA256,
			"content_manifest_sha256": bindings.ContentManifest.SHA256,
			"target_package_sha256":   bindings.TargetPackage.SHA256,
		}},
	}
}
