package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const (
	maxSourceDescriptionArtifact = 8 << 20
	maxMediaEvidenceArtifact     = 16 << 20
	maxTargetPackageArtifact     = 24 << 20
)

type TargetPackageProvider interface {
	PreparePackage(context.Context, sites.TargetPackageMaterial) (sites.PreparedTargetPackage, error)
}

func WithTargetPackages(provider TargetPackageProvider, artifactStore WorkflowArtifactStore) Option {
	return func(runner *Runner) {
		runner.executors["target_package"] = targetPackageExecutor{
			provider: provider, artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type targetPackageExecutor struct {
	provider  TargetPackageProvider
	artifacts WorkflowArtifactStore
	recorder  ArtifactRecorder
}

type targetPackageSnapshot struct {
	JobInput struct {
		TargetPackage json.RawMessage `json:"target_package"`
	} `json:"job_input"`
	ResumeState struct {
		TargetPackage json.RawMessage `json:"target_package"`
	} `json:"resume_state"`
	PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
}

func (executor targetPackageExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("target package workflow dependencies are unavailable")
	}
	material, optionMap, err := executor.material(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	prepared, err := executor.provider.PreparePackage(ctx, material)
	if err != nil {
		return nil, targetPackageBlock(err, material.Target, optionMap)
	}
	body, err := json.MarshalIndent(prepared, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize target package: %w", err)
	}
	if len(body) > maxTargetPackageArtifact {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: "target_package_too_large", Message: "target package exceeds the auditable artifact size limit", SiteCode: material.Target}},
			NextActions: []NextAction{{Action: "reduce_target_materials", Description: "Review unusually large source descriptions or MediaInfo evidence before resuming."}},
			ResumeState: map[string]any{"target_package": optionMap},
		}
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, strings.ToLower(material.Target)+"-target-package.json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("persist target package artifact: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "target_package", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"target": prepared.Target, "adapter": prepared.Adapter,
			"category": prepared.FormFields["category"], "standard": prepared.FormFields["standard"],
			"description_length":     len([]rune(prepared.Description)),
			"screenshot_count":       len(material.Screenshots),
			"manual_review_required": prepared.ManualReviewRequired,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register target package artifact: %w", err)
	}
	return mustJSON(map[string]any{
		"prepared": true, "target": prepared.Target, "adapter": prepared.Adapter,
		"form_fields": prepared.FormFields, "description_length": len([]rune(prepared.Description)),
		"manual_review_required": prepared.ManualReviewRequired,
		"decisions":              prepared.Decisions, "warnings": prepared.Warnings,
		"package_artifact_id": recorded.ID, "package_sha256": recorded.SHA256,
		"package_storage_path": recorded.StoragePath, "package_size_bytes": recorded.SizeBytes,
		"evidence": prepared.Evidence,
	}), nil
}

func (executor targetPackageExecutor) material(snapshotBody json.RawMessage) (sites.TargetPackageMaterial, map[string]any, error) {
	var snapshot targetPackageSnapshot
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return sites.TargetPackageMaterial{}, nil, fmt.Errorf("decode target package snapshot: %w", err)
	}
	options, optionMap, err := mergeTargetPackageOptions(snapshot.JobInput.TargetPackage, snapshot.ResumeState.TargetPackage)
	if err != nil {
		return sites.TargetPackageMaterial{}, nil, err
	}
	var parsed struct {
		Source sites.SourceReference `json:"source"`
		Target string                `json:"target"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "source_parse", &parsed) || parsed.Target == "" {
		return sites.TargetPackageMaterial{}, nil, fmt.Errorf("source_parse target evidence is missing or incomplete")
	}
	var inspected struct {
		SourceInfo          sites.SourceInfo             `json:"source_info"`
		DescriptionArtifact sites.TargetArtifactEvidence `json:"description_artifact"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "source_inspect", &inspected) || inspected.SourceInfo.Tracker == "" {
		return sites.TargetPackageMaterial{}, nil, fmt.Errorf("source_inspect evidence is missing or incomplete")
	}
	var metadata struct {
		Identity            metadataIdentity  `json:"identity"`
		Links               map[string]string `json:"links"`
		MetadataArtifactID  string            `json:"metadata_artifact_id"`
		MetadataSHA256      string            `json:"metadata_sha256"`
		MetadataStoragePath string            `json:"metadata_storage_path"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "metadata", &metadata) || metadata.Identity.Title == "" || metadata.MetadataSHA256 == "" {
		return sites.TargetPackageMaterial{}, nil, fmt.Errorf("metadata evidence is missing or incomplete")
	}
	metadataEvidence := map[string]any{"artifact_id": metadata.MetadataArtifactID, "sha256": metadata.MetadataSHA256, "storage_path": metadata.MetadataStoragePath}
	metadataDescription := ""
	metadataEnrichmentRequired := false
	var tmdbOutput struct {
		Resolved            bool              `json:"resolved"`
		Identity            metadataIdentity  `json:"identity"`
		Links               map[string]string `json:"links"`
		Provider            string            `json:"provider"`
		Adapter             string            `json:"adapter"`
		ConfigurationSHA256 string            `json:"configuration_sha256"`
		QuerySHA256         string            `json:"query_sha256"`
		ArtifactID          string            `json:"artifact_id"`
		ArtifactSHA256      string            `json:"artifact_sha256"`
		ArtifactStoragePath string            `json:"artifact_storage_path"`
	}
	if body, exists := snapshot.PreviousSteps["metadata_tmdb"]; exists {
		metadataEnrichmentRequired = true
		if json.Unmarshal(body, &tmdbOutput) != nil || !tmdbOutput.Resolved || tmdbOutput.Identity.IMDbID == "" ||
			tmdbOutput.Identity.TMDbID == "" || tmdbOutput.ArtifactID == "" || tmdbOutput.ArtifactSHA256 == "" {
			return sites.TargetPackageMaterial{}, nil, fmt.Errorf("metadata_tmdb evidence is missing or incomplete")
		}
		tmdbBody, readErr := readTargetArtifact(executor.artifacts, sites.TargetArtifactEvidence{
			ArtifactID: tmdbOutput.ArtifactID, StoragePath: tmdbOutput.ArtifactStoragePath, SHA256: tmdbOutput.ArtifactSHA256,
		}, 512<<10)
		var tmdbDocument metadataTMDbDocument
		if readErr != nil || json.Unmarshal(tmdbBody, &tmdbDocument) != nil || tmdbDocument.Identity != tmdbOutput.Identity ||
			tmdbDocument.Provider != tmdbOutput.Provider || tmdbDocument.Adapter != tmdbOutput.Adapter ||
			tmdbDocument.ConfigurationSHA256 != tmdbOutput.ConfigurationSHA256 || tmdbDocument.QuerySHA256 != tmdbOutput.QuerySHA256 {
			return sites.TargetPackageMaterial{}, nil, fmt.Errorf("metadata_tmdb artifact verification failed")
		}
		metadata.Identity, metadata.Links = tmdbOutput.Identity, tmdbOutput.Links
		metadataEvidence["tmdb"] = map[string]any{
			"provider": tmdbOutput.Provider, "adapter": tmdbOutput.Adapter, "configuration_sha256": tmdbOutput.ConfigurationSHA256,
			"query_sha256": tmdbOutput.QuerySHA256,
			"artifact_id":  tmdbOutput.ArtifactID, "sha256": tmdbOutput.ArtifactSHA256, "storage_path": tmdbOutput.ArtifactStoragePath,
		}
	}
	var ptgenOutput struct {
		Resolved            bool             `json:"resolved"`
		Identity            metadataIdentity `json:"identity"`
		Provider            string           `json:"provider"`
		Adapter             string           `json:"adapter"`
		ConfigurationSHA256 string           `json:"configuration_sha256"`
		QuerySHA256         string           `json:"query_sha256"`
		DescriptionSHA256   string           `json:"description_sha256"`
		DescriptionSize     int              `json:"description_size_bytes"`
		ArtifactID          string           `json:"artifact_id"`
		ArtifactSHA256      string           `json:"artifact_sha256"`
		ArtifactStoragePath string           `json:"artifact_storage_path"`
	}
	if body, exists := snapshot.PreviousSteps["metadata_ptgen"]; exists {
		metadataEnrichmentRequired = true
		if json.Unmarshal(body, &ptgenOutput) != nil || !ptgenOutput.Resolved || ptgenOutput.Identity != metadata.Identity ||
			ptgenOutput.DescriptionSHA256 == "" || ptgenOutput.ArtifactSHA256 == "" {
			return sites.TargetPackageMaterial{}, nil, fmt.Errorf("metadata_ptgen evidence is missing or inconsistent")
		}
		ptgenBody, readErr := readTargetArtifact(executor.artifacts, sites.TargetArtifactEvidence{
			ArtifactID: ptgenOutput.ArtifactID, StoragePath: ptgenOutput.ArtifactStoragePath, SHA256: ptgenOutput.ArtifactSHA256,
		}, maxPTGenDescriptionBytes+64*1024)
		var ptgenDocument metadataPTGenDocument
		if readErr != nil || json.Unmarshal(ptgenBody, &ptgenDocument) != nil || ptgenDocument.Identity != metadata.Identity ||
			ptgenDocument.Provider != ptgenOutput.Provider || ptgenDocument.Adapter != ptgenOutput.Adapter ||
			ptgenDocument.ConfigurationSHA256 != ptgenOutput.ConfigurationSHA256 || ptgenDocument.QuerySHA256 != ptgenOutput.QuerySHA256 ||
			ptgenDocument.DescriptionSHA256 != ptgenOutput.DescriptionSHA256 || sha256Hex([]byte(ptgenDocument.Description)) != ptgenOutput.DescriptionSHA256 ||
			len([]byte(ptgenDocument.Description)) != ptgenOutput.DescriptionSize {
			return sites.TargetPackageMaterial{}, nil, fmt.Errorf("metadata_ptgen artifact verification failed")
		}
		metadataDescription = ptgenDocument.Description
		metadataEvidence["ptgen"] = map[string]any{
			"provider": ptgenOutput.Provider, "adapter": ptgenOutput.Adapter, "configuration_sha256": ptgenOutput.ConfigurationSHA256,
			"query_sha256":       ptgenOutput.QuerySHA256,
			"description_sha256": ptgenOutput.DescriptionSHA256, "description_size_bytes": ptgenOutput.DescriptionSize,
			"artifact_id": ptgenOutput.ArtifactID, "sha256": ptgenOutput.ArtifactSHA256, "storage_path": ptgenOutput.ArtifactStoragePath,
		}
	} else if metadataEnrichmentRequired {
		return sites.TargetPackageMaterial{}, nil, fmt.Errorf("metadata_ptgen evidence is missing")
	}
	var content struct {
		Resolved            bool   `json:"resolved"`
		DownloaderName      string `json:"downloader_name"`
		TorrentHash         string `json:"torrent_hash"`
		LocalRoot           string `json:"local_root"`
		FileCount           int    `json:"file_count"`
		TotalSizeBytes      int64  `json:"total_size_bytes"`
		ManifestArtifactID  string `json:"manifest_artifact_id"`
		ManifestSHA256      string `json:"manifest_sha256"`
		ManifestStoragePath string `json:"manifest_storage_path"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "content_resolve", &content) || !content.Resolved || content.ManifestSHA256 == "" {
		return sites.TargetPackageMaterial{}, nil, fmt.Errorf("content_resolve evidence is missing or incomplete")
	}
	var mediaOutput struct {
		Kind                string `json:"kind"`
		Tool                string `json:"tool"`
		Version             string `json:"version"`
		DocumentFormat      string `json:"document_format"`
		ArtifactID          string `json:"artifact_id"`
		ArtifactSHA256      string `json:"artifact_sha256"`
		ArtifactStoragePath string `json:"artifact_storage_path"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "media_info", &mediaOutput) || mediaOutput.ArtifactStoragePath == "" || mediaOutput.ArtifactSHA256 == "" {
		return sites.TargetPackageMaterial{}, nil, fmt.Errorf("media_info artifact evidence is missing or incomplete")
	}
	mediaBody, err := readTargetArtifact(executor.artifacts, sites.TargetArtifactEvidence{
		ArtifactID: mediaOutput.ArtifactID, StoragePath: mediaOutput.ArtifactStoragePath,
		SHA256: mediaOutput.ArtifactSHA256,
	}, maxMediaEvidenceArtifact)
	if err != nil || (mediaOutput.Kind == "mediainfo" && !json.Valid(mediaBody)) ||
		(mediaOutput.Kind == "bdinfo" && (!utf8.Valid(mediaBody) || bytes.IndexByte(mediaBody, 0) >= 0 || len(bytes.TrimSpace(mediaBody)) == 0)) ||
		(mediaOutput.Kind != "mediainfo" && mediaOutput.Kind != "bdinfo") {
		return sites.TargetPackageMaterial{}, nil, fmt.Errorf("media inspection artifact verification failed")
	}

	var imageOutput struct {
		Uploaded bool                 `json:"uploaded"`
		Receipts []imageUploadReceipt `json:"receipts"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "image_upload", &imageOutput) || !imageOutput.Uploaded || len(imageOutput.Receipts) == 0 {
		return sites.TargetPackageMaterial{}, nil, fmt.Errorf("image_upload receipt evidence is missing or incomplete")
	}
	screenshots := make([]sites.TargetScreenshotEvidence, 0, len(imageOutput.Receipts))
	for _, receipt := range imageOutput.Receipts {
		if receipt.Upload.Result.URL == "" || receipt.Source.SHA256 == "" || receipt.ReceiptID == "" {
			return sites.TargetPackageMaterial{}, nil, fmt.Errorf("image_upload receipt evidence is incomplete")
		}
		screenshots = append(screenshots, sites.TargetScreenshotEvidence{
			Index: receipt.Index, SourceSHA256: receipt.Source.SHA256,
			ReceiptArtifactID: receipt.ReceiptID, ReceiptSHA256: receipt.ReceiptSHA,
			URL: receipt.Upload.Result.URL, ViewerURL: receipt.Upload.Result.ViewerURL,
		})
	}

	sourceDescription := ""
	if inspected.DescriptionArtifact.StoragePath != "" {
		descriptionBody, readErr := readTargetArtifact(executor.artifacts, inspected.DescriptionArtifact, maxSourceDescriptionArtifact)
		if readErr != nil {
			return sites.TargetPackageMaterial{}, nil, fmt.Errorf("source description artifact verification failed")
		}
		sourceDescription = string(descriptionBody)
	}
	var sourceRules struct {
		Fingerprint string `json:"fingerprint"`
		RevisionID  string `json:"rule_revision_id"`
	}
	_ = decodePrevious(snapshot.PreviousSteps, "source_rules", &sourceRules)
	var sourceTorrent struct {
		ArtifactID string `json:"artifact_id"`
		SHA256     string `json:"sha256"`
		Hashes     any    `json:"hashes"`
	}
	_ = decodePrevious(snapshot.PreviousSteps, "source_torrent", &sourceTorrent)

	material := sites.TargetPackageMaterial{
		Target: parsed.Target, Source: inspected.SourceInfo, Title: metadata.Identity.Title,
		Links: metadata.Links, MetadataDescription: metadataDescription,
		MetadataEnrichmentRequired: metadataEnrichmentRequired, SourceDescription: sourceDescription,
		Content: sites.TargetContentEvidence{
			LocalRoot: content.LocalRoot, FileCount: content.FileCount, TotalSizeBytes: content.TotalSizeBytes,
			ManifestID: content.ManifestArtifactID, ManifestSHA256: content.ManifestSHA256,
			DownloaderName: content.DownloaderName, SourceTorrentHash: content.TorrentHash,
		},
		Media: sites.TargetMediaEvidence{
			Kind: mediaOutput.Kind, Tool: mediaOutput.Tool, Version: mediaOutput.Version,
			Format: mediaOutput.DocumentFormat, Document: string(mediaBody), Artifact: sites.TargetArtifactEvidence{
				ArtifactID: mediaOutput.ArtifactID, StoragePath: mediaOutput.ArtifactStoragePath, SHA256: mediaOutput.ArtifactSHA256,
			},
		},
		Screenshots: screenshots, Options: options,
		Evidence: map[string]any{
			"source_rule":         map[string]any{"revision_id": sourceRules.RevisionID, "fingerprint": sourceRules.Fingerprint},
			"source_torrent":      map[string]any{"artifact_id": sourceTorrent.ArtifactID, "sha256": sourceTorrent.SHA256, "hashes": sourceTorrent.Hashes},
			"source_description":  inspected.DescriptionArtifact,
			"metadata":            metadataEvidence,
			"content_manifest":    map[string]any{"artifact_id": content.ManifestArtifactID, "sha256": content.ManifestSHA256, "storage_path": content.ManifestStoragePath},
			"media_info":          map[string]any{"artifact_id": mediaOutput.ArtifactID, "sha256": mediaOutput.ArtifactSHA256, "storage_path": mediaOutput.ArtifactStoragePath},
			"screenshot_receipts": screenshots,
		},
	}
	return material, optionMap, nil
}

func decodePrevious(previous map[string]json.RawMessage, key string, target any) bool {
	body, exists := previous[key]
	return exists && json.Unmarshal(body, target) == nil
}

func mergeTargetPackageOptions(base, resumed json.RawMessage) (json.RawMessage, map[string]any, error) {
	result := map[string]any{}
	for _, body := range []json.RawMessage{base, resumed} {
		if len(bytes.TrimSpace(body)) == 0 || bytes.Equal(bytes.TrimSpace(body), []byte("null")) {
			continue
		}
		var values map[string]any
		if err := json.Unmarshal(body, &values); err != nil || values == nil {
			return nil, nil, fmt.Errorf("target_package options must be a JSON object")
		}
		for key, value := range values {
			result[key] = value
		}
	}
	body, err := json.Marshal(result)
	return body, result, err
}

func readTargetArtifact(store ArtifactReader, evidence sites.TargetArtifactEvidence, limit int64) ([]byte, error) {
	if evidence.StoragePath == "" || evidence.SHA256 == "" {
		return nil, fmt.Errorf("artifact path and SHA-256 are required")
	}
	file, err := store.Open(evidence.StoragePath)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil || int64(len(body)) > limit {
		return nil, fmt.Errorf("artifact is unreadable or too large")
	}
	if evidence.SizeBytes > 0 && int64(len(body)) != evidence.SizeBytes {
		return nil, fmt.Errorf("artifact size does not match evidence")
	}
	digest := sha256.Sum256(body)
	if !strings.EqualFold(hex.EncodeToString(digest[:]), evidence.SHA256) {
		return nil, fmt.Errorf("artifact hash does not match evidence")
	}
	return body, nil
}

func targetPackageBlock(err error, target string, options map[string]any) *BlockError {
	var requirements *sites.PackageRequirementsError
	if errors.As(err, &requirements) {
		blockers := make([]Blocker, 0, len(requirements.Requirements))
		for _, requirement := range requirements.Requirements {
			blockers = append(blockers, Blocker{Code: requirement.Code, Message: requirement.Message, SiteCode: target})
		}
		return &BlockError{
			Blockers: blockers,
			NextActions: []NextAction{{
				Action:      "provide_target_package_fields",
				Description: "Supply only explicitly reviewed target fields in resume_state.target_package, then resume this step.",
				Parameters:  map[string]any{"site_code": target, "requirements": requirements.Requirements},
			}},
			ResumeState: map[string]any{"target_package": options, "target_package_requirements": requirements.Requirements},
		}
	}
	code, message, _ := sites.ErrorDetails(err)
	return &BlockError{
		Blockers:    []Blocker{{Code: code, Message: message, SiteCode: target}},
		NextActions: []NextAction{{Action: "review_target_package_configuration", Description: "Correct the target adapter inputs or implement the target package adapter before resuming.", Parameters: map[string]any{"site_code": target}}},
		ResumeState: map[string]any{"target_package": options},
	}
}
