package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const (
	maxSummaryArtifacts = 10_000
	maxSummaryBytes     = 8 << 20
)

type SummaryCatalog interface {
	ArtifactRecorder
	ListArtifacts(context.Context, string) ([]workflow.Artifact, error)
}

func WithSummary(artifactStore ArtifactWriter) Option {
	return func(runner *Runner) {
		runner.executors["summary"] = summaryExecutor{
			artifacts: artifactStore, catalog: runner.runtime, now: time.Now,
		}
	}
}

type summaryExecutor struct {
	artifacts ArtifactWriter
	catalog   SummaryCatalog
	now       func() time.Time
}

type summaryArtifact struct {
	ArtifactID  string `json:"artifact_id"`
	Kind        string `json:"kind"`
	StoragePath string `json:"storage_path"`
	MIMEType    string `json:"mime_type,omitempty"`
	SizeBytes   int64  `json:"size_bytes"`
	SHA256      string `json:"sha256"`
}

type summaryRule struct {
	SiteCode      string        `json:"site_code"`
	Role          string        `json:"role"`
	RevisionID    string        `json:"rule_revision_id"`
	Fingerprint   string        `json:"fingerprint"`
	Accepted      bool          `json:"accepted"`
	AcceptanceSHA string        `json:"acceptance_sha256"`
	Limits        rules.Limits  `json:"limits"`
	Seeding       rules.Seeding `json:"seeding"`
}

type summaryBindings struct {
	Source struct {
		Tracker   string `json:"tracker"`
		TorrentID string `json:"torrent_id"`
	}
	Target        string
	SourceInfo    sites.SourceInfo
	SourceRule    summaryRule
	TargetRule    summaryRule
	SourceTorrent struct {
		ArtifactID  string                 `json:"artifact_id"`
		StoragePath string                 `json:"storage_path"`
		SizeBytes   int64                  `json:"size_bytes"`
		SHA256      string                 `json:"sha256"`
		Hashes      torrentmeta.InfoHashes `json:"hashes"`
	}
	SourceAdd struct {
		DownloaderName string `json:"downloader_name"`
		Adapter        string `json:"downloader_adapter"`
		TorrentHash    string `json:"torrent_hash"`
		Limits         any    `json:"limits"`
		Options        any    `json:"options"`
	}
	Content struct {
		Resolved       bool   `json:"resolved"`
		DownloaderName string `json:"downloader_name"`
		TorrentHash    string `json:"torrent_hash"`
		LocalRoot      string `json:"local_root"`
		RemoteRoot     string `json:"remote_root"`
		FileCount      int    `json:"file_count"`
		TotalSize      int64  `json:"total_size_bytes"`
		ArtifactID     string `json:"manifest_artifact_id"`
		SHA256         string `json:"manifest_sha256"`
		StoragePath    string `json:"manifest_storage_path"`
	}
	Metadata struct {
		Identity     any    `json:"identity"`
		Links        any    `json:"links"`
		Strength     string `json:"identity_strength"`
		ManualReview bool   `json:"manual_review_required"`
		ArtifactID   string `json:"metadata_artifact_id"`
		SHA256       string `json:"metadata_sha256"`
		StoragePath  string `json:"metadata_storage_path"`
	}
	MetadataEnriched bool
	MetadataTMDb     struct {
		Resolved      bool             `json:"resolved"`
		Identity      metadataIdentity `json:"identity"`
		Provider      string           `json:"provider"`
		Adapter       string           `json:"adapter"`
		Configuration string           `json:"configuration_sha256"`
		QuerySHA256   string           `json:"query_sha256"`
		ArtifactID    string           `json:"artifact_id"`
		SHA256        string           `json:"artifact_sha256"`
		StoragePath   string           `json:"artifact_storage_path"`
	}
	MetadataPTGen struct {
		Resolved        bool             `json:"resolved"`
		Identity        metadataIdentity `json:"identity"`
		Provider        string           `json:"provider"`
		Adapter         string           `json:"adapter"`
		Configuration   string           `json:"configuration_sha256"`
		QuerySHA256     string           `json:"query_sha256"`
		DescriptionSHA  string           `json:"description_sha256"`
		DescriptionSize int64            `json:"description_size_bytes"`
		ArtifactID      string           `json:"artifact_id"`
		SHA256          string           `json:"artifact_sha256"`
		StoragePath     string           `json:"artifact_storage_path"`
	}
	MediaInfo struct {
		Kind        string `json:"kind"`
		Tool        string `json:"tool"`
		Version     string `json:"version"`
		Selected    string `json:"selected_path"`
		ArtifactID  string `json:"artifact_id"`
		SHA256      string `json:"artifact_sha256"`
		StoragePath string `json:"artifact_storage_path"`
	}
	Screenshots struct {
		Generated bool                      `json:"generated"`
		Count     int                       `json:"screenshot_count"`
		Profile   any                       `json:"profile"`
		Artifacts []screenshotArtifactInput `json:"artifacts"`
	}
	Images struct {
		Uploaded bool                 `json:"uploaded"`
		Count    int                  `json:"image_count"`
		Receipts []imageUploadReceipt `json:"receipts"`
	}
	Package struct {
		Prepared    bool   `json:"prepared"`
		Target      string `json:"target"`
		Adapter     string `json:"adapter"`
		ArtifactID  string `json:"package_artifact_id"`
		SHA256      string `json:"package_sha256"`
		StoragePath string `json:"package_storage_path"`
		SizeBytes   int64  `json:"package_size_bytes"`
	}
	Duplicate struct {
		Checked       bool   `json:"checked"`
		Status        string `json:"status"`
		Target        string `json:"target"`
		Duplicate     bool   `json:"duplicate"`
		ResultCount   int    `json:"result_count"`
		ArtifactID    string `json:"duplicate_check_artifact_id"`
		SHA256        string `json:"duplicate_check_sha256"`
		StoragePath   string `json:"duplicate_check_storage_path"`
		Configuration string `json:"configuration_sha256"`
	}
	TargetTorrent struct {
		Prepared    bool                   `json:"prepared"`
		Verified    bool                   `json:"verified"`
		Status      string                 `json:"status"`
		Target      string                 `json:"target"`
		Tool        string                 `json:"tool"`
		ToolVersion string                 `json:"tool_version"`
		ArtifactID  string                 `json:"target_torrent_artifact_id"`
		StoragePath string                 `json:"target_torrent_storage_path"`
		SHA256      string                 `json:"target_torrent_sha256"`
		SizeBytes   int64                  `json:"target_torrent_size_bytes"`
		Hashes      torrentmeta.InfoHashes `json:"target_torrent_hashes"`
		Fingerprint string                 `json:"content_fingerprint_sha256"`
		ReceiptID   string                 `json:"receipt_artifact_id"`
		ReceiptSHA  string                 `json:"receipt_sha256"`
		ReceiptPath string                 `json:"receipt_storage_path"`
	}
	Upload struct {
		Uploaded      bool                   `json:"uploaded"`
		Status        string                 `json:"status"`
		Target        string                 `json:"target"`
		TorrentID     string                 `json:"uploaded_torrent_id"`
		DetailsURL    string                 `json:"details_url"`
		SubmittedAt   time.Time              `json:"submitted_at"`
		ResponseSHA   string                 `json:"response_sha256"`
		Configuration string                 `json:"configuration_sha256"`
		SubmittedSHA  string                 `json:"submitted_torrent_sha256"`
		SubmittedHash torrentmeta.InfoHashes `json:"submitted_infohashes"`
		FreshDupeID   string                 `json:"preupload_duplicate_check_artifact_id"`
		FreshDupeSHA  string                 `json:"preupload_duplicate_check_sha256"`
		ReceiptID     string                 `json:"upload_receipt_artifact_id"`
		ReceiptSHA    string                 `json:"upload_receipt_sha256"`
		ReceiptPath   string                 `json:"upload_receipt_storage_path"`
	}
	Downloaded struct {
		Downloaded    bool                   `json:"downloaded"`
		Verified      bool                   `json:"verified"`
		Status        string                 `json:"status"`
		Target        string                 `json:"target"`
		TorrentID     string                 `json:"uploaded_torrent_id"`
		ArtifactID    string                 `json:"target_torrent_artifact_id"`
		StoragePath   string                 `json:"target_torrent_storage_path"`
		SHA256        string                 `json:"target_torrent_sha256"`
		SizeBytes     int64                  `json:"target_torrent_size_bytes"`
		Hashes        torrentmeta.InfoHashes `json:"target_torrent_hashes"`
		Fingerprint   string                 `json:"content_fingerprint_sha256"`
		AnnounceSHA   string                 `json:"announce_sha256"`
		Configuration string                 `json:"configuration_sha256"`
		ReceiptID     string                 `json:"receipt_artifact_id"`
		ReceiptSHA    string                 `json:"receipt_sha256"`
		ReceiptPath   string                 `json:"receipt_storage_path"`
	}
	Injection struct {
		Injected       bool                       `json:"injected"`
		Status         string                     `json:"status"`
		Target         string                     `json:"target"`
		TorrentID      string                     `json:"uploaded_torrent_id"`
		DownloaderName string                     `json:"downloader_name"`
		Adapter        string                     `json:"downloader_adapter"`
		Configuration  string                     `json:"downloader_configuration_sha256"`
		TorrentHash    string                     `json:"torrent_hash"`
		Options        targetInjectOptionsReceipt `json:"options"`
		Rule           targetInjectRuleReceipt    `json:"rule"`
		ExpectedPath   string                     `json:"expected_remote_content_path"`
		ReceiptID      string                     `json:"receipt_artifact_id"`
		ReceiptSHA     string                     `json:"receipt_sha256"`
		ReceiptPath    string                     `json:"receipt_storage_path"`
	}
	Seed struct {
		Verified        bool             `json:"verified"`
		Status          string           `json:"status"`
		Target          string           `json:"target"`
		TorrentID       string           `json:"uploaded_torrent_id"`
		DownloaderName  string           `json:"downloader_name"`
		TorrentHash     string           `json:"torrent_hash"`
		Requirements    rules.Seeding    `json:"requirements"`
		Checks          targetSeedChecks `json:"checks"`
		ObservationID   string           `json:"observation_artifact_id"`
		ObservationSHA  string           `json:"observation_sha256"`
		ObservationPath string           `json:"observation_storage_path"`
	}
}

func (executor summaryExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.artifacts == nil || executor.catalog == nil {
		return nil, fmt.Errorf("summary workflow dependencies are unavailable")
	}
	bindings, err := decodeSummaryBindings(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	workflowArtifacts, err := executor.catalog.ListArtifacts(ctx, execution.Job.ID)
	if err != nil {
		return nil, fmt.Errorf("list workflow artifacts for summary: %w", err)
	}
	artifactRefs, artifactIndex, kindCounts, err := summarizeArtifacts(execution.Job.ID, workflowArtifacts)
	if err != nil {
		return nil, summaryEvidenceBlock(err)
	}
	if err := validateSummaryBindings(bindings, artifactIndex, kindCounts); err != nil {
		return nil, summaryEvidenceBlock(err)
	}
	now := time.Now().UTC()
	if executor.now != nil {
		now = executor.now().UTC()
	}
	document := map[string]any{
		"schema_version": 1, "kind": "upload-assistant.retorrent-summary.v1",
		"ok": true, "status": "complete", "job_id": execution.Job.ID, "generated_at": now,
		"source": map[string]any{
			"site_code": bindings.Source.Tracker, "torrent_id": bindings.Source.TorrentID,
			"name": bindings.SourceInfo.Name, "free": bindings.SourceInfo.Free,
			"promotion_labels": bindings.SourceInfo.PromotionLabels,
			"torrent": map[string]any{
				"artifact_id": bindings.SourceTorrent.ArtifactID, "storage_path": bindings.SourceTorrent.StoragePath,
				"sha256": bindings.SourceTorrent.SHA256, "size_bytes": bindings.SourceTorrent.SizeBytes,
				"hashes": bindings.SourceTorrent.Hashes,
			},
			"rules": bindings.SourceRule,
			"downloader": map[string]any{
				"name": bindings.SourceAdd.DownloaderName, "adapter": bindings.SourceAdd.Adapter, "torrent_hash": bindings.SourceAdd.TorrentHash,
				"limits": bindings.SourceAdd.Limits, "options": bindings.SourceAdd.Options,
			},
		},
		"content": map[string]any{
			"file_count": bindings.Content.FileCount, "total_size_bytes": bindings.Content.TotalSize,
			"local_root": bindings.Content.LocalRoot, "remote_root": bindings.Content.RemoteRoot,
			"manifest": map[string]any{"artifact_id": bindings.Content.ArtifactID, "storage_path": bindings.Content.StoragePath, "sha256": bindings.Content.SHA256},
		},
		"materials": map[string]any{
			"metadata": map[string]any{
				"identity": bindings.Metadata.Identity, "links": bindings.Metadata.Links,
				"identity_strength": bindings.Metadata.Strength, "manual_review_required": bindings.Metadata.ManualReview,
				"artifact_id": bindings.Metadata.ArtifactID, "storage_path": bindings.Metadata.StoragePath, "sha256": bindings.Metadata.SHA256,
			},
			"media_info": map[string]any{
				"kind": bindings.MediaInfo.Kind, "tool": bindings.MediaInfo.Tool, "version": bindings.MediaInfo.Version,
				"selected_path": bindings.MediaInfo.Selected, "artifact_id": bindings.MediaInfo.ArtifactID,
				"storage_path": bindings.MediaInfo.StoragePath, "sha256": bindings.MediaInfo.SHA256,
			},
			"metadata_enrichment": map[string]any{
				"required": bindings.MetadataEnriched,
				"tmdb": map[string]any{
					"resolved": bindings.MetadataTMDb.Resolved, "provider": bindings.MetadataTMDb.Provider,
					"identity": bindings.MetadataTMDb.Identity,
					"adapter":  bindings.MetadataTMDb.Adapter, "configuration_sha256": bindings.MetadataTMDb.Configuration,
					"query_sha256": bindings.MetadataTMDb.QuerySHA256, "artifact_id": bindings.MetadataTMDb.ArtifactID,
					"storage_path": bindings.MetadataTMDb.StoragePath, "sha256": bindings.MetadataTMDb.SHA256,
				},
				"ptgen": map[string]any{
					"resolved": bindings.MetadataPTGen.Resolved, "provider": bindings.MetadataPTGen.Provider,
					"identity": bindings.MetadataPTGen.Identity,
					"adapter":  bindings.MetadataPTGen.Adapter, "configuration_sha256": bindings.MetadataPTGen.Configuration,
					"query_sha256": bindings.MetadataPTGen.QuerySHA256, "description_sha256": bindings.MetadataPTGen.DescriptionSHA,
					"description_size_bytes": bindings.MetadataPTGen.DescriptionSize, "artifact_id": bindings.MetadataPTGen.ArtifactID,
					"storage_path": bindings.MetadataPTGen.StoragePath, "sha256": bindings.MetadataPTGen.SHA256,
				},
			},
			"screenshots":   map[string]any{"count": bindings.Screenshots.Count, "profile": bindings.Screenshots.Profile},
			"image_uploads": map[string]any{"count": bindings.Images.Count},
		},
		"target": map[string]any{
			"site_code": bindings.Target, "torrent_id": bindings.Upload.TorrentID, "details_url": bindings.Upload.DetailsURL,
			"rules": bindings.TargetRule,
			"duplicate_check": map[string]any{
				"status": "clear", "initial_result_count": bindings.Duplicate.ResultCount,
				"initial_artifact_id": bindings.Duplicate.ArtifactID, "initial_sha256": bindings.Duplicate.SHA256,
				"preupload_artifact_id": bindings.Upload.FreshDupeID, "preupload_sha256": bindings.Upload.FreshDupeSHA,
			},
			"package": map[string]any{
				"adapter": bindings.Package.Adapter, "artifact_id": bindings.Package.ArtifactID,
				"storage_path": bindings.Package.StoragePath, "sha256": bindings.Package.SHA256,
			},
			"submitted_torrent": map[string]any{
				"artifact_id": bindings.TargetTorrent.ArtifactID, "storage_path": bindings.TargetTorrent.StoragePath,
				"sha256": bindings.TargetTorrent.SHA256, "hashes": bindings.TargetTorrent.Hashes,
				"content_fingerprint_sha256": bindings.TargetTorrent.Fingerprint,
				"tool":                       bindings.TargetTorrent.Tool, "tool_version": bindings.TargetTorrent.ToolVersion,
			},
			"upload": map[string]any{
				"submitted_at": bindings.Upload.SubmittedAt, "response_sha256": bindings.Upload.ResponseSHA,
				"configuration_sha256": bindings.Upload.Configuration,
				"receipt_artifact_id":  bindings.Upload.ReceiptID, "receipt_sha256": bindings.Upload.ReceiptSHA,
			},
			"downloaded_torrent": map[string]any{
				"artifact_id": bindings.Downloaded.ArtifactID, "storage_path": bindings.Downloaded.StoragePath,
				"sha256": bindings.Downloaded.SHA256, "hashes": bindings.Downloaded.Hashes,
				"content_fingerprint_sha256": bindings.Downloaded.Fingerprint,
				"announce_sha256":            bindings.Downloaded.AnnounceSHA, "configuration_sha256": bindings.Downloaded.Configuration,
			},
		},
		"seeding": map[string]any{
			"downloader_name": bindings.Injection.DownloaderName, "downloader_adapter": bindings.Injection.Adapter, "downloader_configuration_sha256": bindings.Injection.Configuration,
			"torrent_hash": bindings.Injection.TorrentHash, "expected_remote_content_path": bindings.Injection.ExpectedPath,
			"apply_labels": bindings.Injection.Options.ApplyLabels, "category": bindings.Injection.Options.Category, "tags": bindings.Injection.Options.Tags,
			"download_limit_bytes_per_second": bindings.Injection.Options.DownloadLimit,
			"upload_limit_bytes_per_second":   bindings.Injection.Options.UploadLimit,
			"requirements":                    bindings.Seed.Requirements, "checks": bindings.Seed.Checks,
			"observation_artifact_id": bindings.Seed.ObservationID, "observation_sha256": bindings.Seed.ObservationSHA,
		},
		"audit": map[string]any{
			"artifact_count": len(artifactRefs), "artifacts": artifactRefs,
			"required_gates": []string{"source_rules", "target_duplicate_check", "target_rules", "confirm_upload", "target_seed_verify"},
		},
		"blockers": []any{},
		"next_actions": []map[string]any{{
			"action": "monitor_target_seeding", "description": "Keep the target torrent active and continue observing tracker-specific long-term seeding obligations.",
			"parameters": map[string]any{"downloader_name": bindings.Injection.DownloaderName, "torrent_hash": bindings.Injection.TorrentHash},
		}},
		"resume": map[string]any{"resumable": false, "reason": "workflow_complete"},
	}
	body, err := json.MarshalIndent(document, "", "  ")
	if err != nil || len(body) > maxSummaryBytes {
		return nil, fmt.Errorf("serialize retorrent summary: summary exceeds the bounded size")
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, "retorrent-summary.json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("persist retorrent summary: %w", err)
	}
	recorded, err := executor.catalog.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "job_summary", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"status": "complete", "source_site": bindings.Source.Tracker, "source_torrent_id": bindings.Source.TorrentID,
			"target_site": bindings.Target, "target_torrent_id": bindings.Upload.TorrentID,
			"target_torrent_hash": bindings.Injection.TorrentHash, "artifact_count": len(artifactRefs),
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register retorrent summary: %w", err)
	}
	document["summary_file"] = summaryArtifact{
		ArtifactID: recorded.ID, Kind: recorded.Kind, StoragePath: recorded.StoragePath,
		MIMEType: recorded.MIMEType, SizeBytes: recorded.SizeBytes, SHA256: recorded.SHA256,
	}
	return json.Marshal(document)
}

func decodeSummaryBindings(snapshotBody json.RawMessage) (summaryBindings, error) {
	var snapshot struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return summaryBindings{}, fmt.Errorf("decode summary snapshot: %w", err)
	}
	var bindings summaryBindings
	var parsed struct {
		Source struct {
			Tracker   string `json:"tracker"`
			TorrentID string `json:"torrent_id"`
		} `json:"source"`
		Target string `json:"target"`
	}
	if err := decodeSummaryStep(snapshot.PreviousSteps, "source_parse", &parsed); err != nil {
		return bindings, err
	}
	bindings.Source, bindings.Target = parsed.Source, strings.ToUpper(strings.TrimSpace(parsed.Target))
	var inspected struct {
		SourceInfo sites.SourceInfo `json:"source_info"`
	}
	if err := decodeSummaryStep(snapshot.PreviousSteps, "source_inspect", &inspected); err != nil {
		return bindings, err
	}
	bindings.SourceInfo = inspected.SourceInfo
	steps := []struct {
		key    string
		target any
	}{
		{"source_rules", &bindings.SourceRule}, {"source_torrent", &bindings.SourceTorrent},
		{"downloader_add", &bindings.SourceAdd}, {"content_resolve", &bindings.Content},
		{"metadata", &bindings.Metadata}, {"media_info", &bindings.MediaInfo},
		{"screenshots", &bindings.Screenshots}, {"image_upload", &bindings.Images},
		{"target_package", &bindings.Package}, {"target_duplicate_check", &bindings.Duplicate},
		{"target_rules", &bindings.TargetRule}, {"target_torrent", &bindings.TargetTorrent},
		{"target_upload", &bindings.Upload}, {"target_torrent_download", &bindings.Downloaded},
		{"target_inject", &bindings.Injection}, {"target_seed_verify", &bindings.Seed},
	}
	for _, step := range steps {
		if err := decodeSummaryStep(snapshot.PreviousSteps, step.key, step.target); err != nil {
			return bindings, err
		}
	}
	if _, exists := snapshot.PreviousSteps["metadata_tmdb"]; exists {
		bindings.MetadataEnriched = true
		if err := decodeSummaryStep(snapshot.PreviousSteps, "metadata_tmdb", &bindings.MetadataTMDb); err != nil {
			return bindings, err
		}
		if err := decodeSummaryStep(snapshot.PreviousSteps, "metadata_ptgen", &bindings.MetadataPTGen); err != nil {
			return bindings, err
		}
	}
	var waited struct {
		Completed bool `json:"completed"`
	}
	if err := decodeSummaryStep(snapshot.PreviousSteps, "downloader_wait", &waited); err != nil || !waited.Completed {
		return bindings, fmt.Errorf("completed downloader_wait evidence is missing")
	}
	return bindings, nil
}

func decodeSummaryStep(previous map[string]json.RawMessage, key string, target any) error {
	body, exists := previous[key]
	if !exists || json.Unmarshal(body, target) != nil {
		return fmt.Errorf("completed %s evidence is missing or invalid", key)
	}
	return nil
}

func summarizeArtifacts(jobID string, input []workflow.Artifact) ([]summaryArtifact, map[string]workflow.Artifact, map[string]int, error) {
	if len(input) == 0 || len(input) > maxSummaryArtifacts {
		return nil, nil, nil, fmt.Errorf("workflow artifact count is missing or exceeds %d", maxSummaryArtifacts)
	}
	refs := make([]summaryArtifact, 0, len(input))
	index := make(map[string]workflow.Artifact, len(input))
	kindCounts := make(map[string]int)
	for _, artifact := range input {
		if artifact.JobID != jobID || artifact.ID == "" || artifact.Kind == "" || artifact.StoragePath == "" ||
			len(artifact.SHA256) != 64 || artifact.SizeBytes < 0 {
			return nil, nil, nil, fmt.Errorf("workflow artifact evidence is incomplete or belongs to another job")
		}
		if _, duplicate := index[artifact.ID]; duplicate {
			return nil, nil, nil, fmt.Errorf("workflow artifact id is duplicated")
		}
		index[artifact.ID] = artifact
		kindCounts[artifact.Kind]++
		refs = append(refs, summaryArtifact{
			ArtifactID: artifact.ID, Kind: artifact.Kind, StoragePath: artifact.StoragePath,
			MIMEType: artifact.MIMEType, SizeBytes: artifact.SizeBytes, SHA256: artifact.SHA256,
		})
	}
	sort.Slice(refs, func(left, right int) bool {
		if refs[left].Kind == refs[right].Kind {
			return refs[left].ArtifactID < refs[right].ArtifactID
		}
		return refs[left].Kind < refs[right].Kind
	})
	return refs, index, kindCounts, nil
}

func validateSummaryBindings(bindings summaryBindings, artifacts map[string]workflow.Artifact, kindCounts map[string]int) error {
	if bindings.Source.Tracker == "" || bindings.Source.TorrentID == "" || bindings.Target == "" ||
		bindings.SourceInfo.Tracker != bindings.Source.Tracker || bindings.SourceInfo.TorrentID != bindings.Source.TorrentID {
		return fmt.Errorf("source identity evidence is incomplete or inconsistent")
	}
	if !validSummaryRule(bindings.SourceRule, bindings.Source.Tracker, "source") || !validSummaryRule(bindings.TargetRule, bindings.Target, "target") {
		return fmt.Errorf("accepted source or target rule evidence is incomplete")
	}
	if bindings.SourceAdd.DownloaderName == "" || bindings.SourceAdd.Adapter == "" || bindings.SourceAdd.DownloaderName != bindings.Content.DownloaderName ||
		!bindings.Content.Resolved || bindings.Content.FileCount <= 0 || bindings.Content.TotalSize <= 0 ||
		(bindings.MediaInfo.Kind != "mediainfo" && bindings.MediaInfo.Kind != "bdinfo") ||
		!bindings.Package.Prepared || bindings.Package.Target != bindings.Target || !bindings.Duplicate.Checked ||
		bindings.Duplicate.Status != "clean" || bindings.Duplicate.Duplicate || bindings.Duplicate.Target != bindings.Target {
		return fmt.Errorf("content, target package, or duplicate gate evidence is incomplete")
	}
	if !bindings.TargetTorrent.Prepared || !bindings.TargetTorrent.Verified || bindings.TargetTorrent.Status != "ready_for_upload" ||
		bindings.TargetTorrent.Target != bindings.Target || !bindings.Upload.Uploaded || bindings.Upload.Status != "uploaded" ||
		bindings.Upload.Target != bindings.Target || bindings.Upload.TorrentID == "" ||
		bindings.Upload.DetailsURL != "https://kp.m-team.cc/details/"+bindings.Upload.TorrentID ||
		bindings.Upload.SubmittedSHA != bindings.TargetTorrent.SHA256 || bindings.Upload.SubmittedHash != bindings.TargetTorrent.Hashes {
		return fmt.Errorf("target torrent or upload evidence is incomplete or inconsistent")
	}
	if !bindings.Downloaded.Downloaded || !bindings.Downloaded.Verified || bindings.Downloaded.Status != "ready_for_injection" ||
		bindings.Downloaded.Target != bindings.Target || bindings.Downloaded.TorrentID != bindings.Upload.TorrentID ||
		bindings.Downloaded.Hashes != bindings.TargetTorrent.Hashes || bindings.Downloaded.Fingerprint != bindings.TargetTorrent.Fingerprint ||
		!bindings.Injection.Injected || bindings.Injection.Status != "injected" || bindings.Injection.Target != bindings.Target || bindings.Injection.Adapter == "" ||
		bindings.Injection.TorrentID != bindings.Upload.TorrentID || !hashMatches(bindings.Injection.TorrentHash, bindings.Downloaded.Hashes) ||
		!bindings.Seed.Verified || bindings.Seed.Status != "seeding_requirements_satisfied" || bindings.Seed.Target != bindings.Target ||
		bindings.Seed.TorrentID != bindings.Upload.TorrentID || bindings.Seed.TorrentHash != bindings.Injection.TorrentHash {
		return fmt.Errorf("download, injection, or seeding evidence is incomplete or inconsistent")
	}
	if !bindings.Seed.Checks.DownloaderMatches || !bindings.Seed.Checks.HashMatches || !bindings.Seed.Checks.Complete ||
		!bindings.Seed.Checks.SeedingState || !bindings.Seed.Checks.ContentPathMatches || !bindings.Seed.Checks.FileManifestMatches ||
		!bindings.Seed.Checks.CategoryMatches || !bindings.Seed.Checks.TagsMatch || !bindings.Seed.Checks.DownloadLimitSafe ||
		!bindings.Seed.Checks.UploadLimitSafe || !bindings.Seed.Checks.TimeRequirementMet || !bindings.Seed.Checks.RatioRequirementMet {
		return fmt.Errorf("target seeding checks are not all satisfied")
	}
	bindingsToCheck := []struct {
		id, sha, kind string
	}{
		{bindings.SourceTorrent.ArtifactID, bindings.SourceTorrent.SHA256, "source_torrent"},
		{bindings.Content.ArtifactID, bindings.Content.SHA256, "content_manifest"},
		{bindings.Metadata.ArtifactID, bindings.Metadata.SHA256, "metadata"},
		{bindings.MediaInfo.ArtifactID, bindings.MediaInfo.SHA256, bindings.MediaInfo.Kind},
		{bindings.Package.ArtifactID, bindings.Package.SHA256, "target_package"},
		{bindings.Duplicate.ArtifactID, bindings.Duplicate.SHA256, "duplicate_check"},
		{bindings.TargetTorrent.ArtifactID, bindings.TargetTorrent.SHA256, "target_torrent"},
		{bindings.TargetTorrent.ReceiptID, bindings.TargetTorrent.ReceiptSHA, "target_torrent_receipt"},
		{bindings.Upload.FreshDupeID, bindings.Upload.FreshDupeSHA, "preupload_duplicate_check"},
		{bindings.Upload.ReceiptID, bindings.Upload.ReceiptSHA, "target_upload_receipt"},
		{bindings.Downloaded.ArtifactID, bindings.Downloaded.SHA256, "target_downloaded_torrent"},
		{bindings.Downloaded.ReceiptID, bindings.Downloaded.ReceiptSHA, "target_torrent_download_receipt"},
		{bindings.Injection.ReceiptID, bindings.Injection.ReceiptSHA, "target_injection_receipt"},
		{bindings.Seed.ObservationID, bindings.Seed.ObservationSHA, "target_seed_observation"},
	}
	if bindings.MetadataEnriched {
		if !bindings.MetadataTMDb.Resolved || !bindings.MetadataPTGen.Resolved || bindings.MetadataTMDb.ArtifactID == "" ||
			bindings.MetadataPTGen.ArtifactID == "" || len(bindings.MetadataPTGen.DescriptionSHA) != 64 || bindings.MetadataPTGen.DescriptionSize <= 0 {
			return fmt.Errorf("TMDb or PTGen enrichment evidence is incomplete")
		}
		if bindings.MetadataTMDb.Identity != bindings.MetadataPTGen.Identity || bindings.MetadataPTGen.Identity.IMDbID == "" ||
			bindings.MetadataPTGen.Identity.TMDbID == "" || bindings.MetadataPTGen.Identity.DoubanID == "" ||
			validateMetadataIdentity(bindings.MetadataPTGen.Identity) != nil {
			return fmt.Errorf("TMDb and PTGen enrichment identities are incomplete or inconsistent")
		}
		bindingsToCheck = append(bindingsToCheck,
			struct{ id, sha, kind string }{bindings.MetadataTMDb.ArtifactID, bindings.MetadataTMDb.SHA256, "metadata_tmdb"},
			struct{ id, sha, kind string }{bindings.MetadataPTGen.ArtifactID, bindings.MetadataPTGen.SHA256, "metadata_ptgen"},
		)
	}
	for _, binding := range bindingsToCheck {
		artifact, exists := artifacts[binding.id]
		if !exists || artifact.Kind != binding.kind || !strings.EqualFold(artifact.SHA256, binding.sha) {
			return fmt.Errorf("required %s artifact binding is missing or inconsistent", binding.kind)
		}
	}
	for _, screenshot := range bindings.Screenshots.Artifacts {
		artifact, exists := artifacts[screenshot.ArtifactID]
		if !exists || artifact.Kind != "screenshot" || artifact.SHA256 != screenshot.SHA256 {
			return fmt.Errorf("screenshot artifact binding is missing or inconsistent")
		}
	}
	for _, receipt := range bindings.Images.Receipts {
		artifact, exists := artifacts[receipt.ReceiptID]
		if !exists || artifact.Kind != "image_upload_receipt" || artifact.SHA256 != receipt.ReceiptSHA {
			return fmt.Errorf("image upload receipt binding is missing or inconsistent")
		}
	}
	if !bindings.Screenshots.Generated || !bindings.Images.Uploaded || bindings.Screenshots.Count <= 0 ||
		bindings.Images.Count != bindings.Screenshots.Count || len(bindings.Screenshots.Artifacts) != bindings.Screenshots.Count ||
		len(bindings.Images.Receipts) != bindings.Images.Count || kindCounts["screenshot"] != bindings.Screenshots.Count ||
		kindCounts["image_upload_receipt"] != bindings.Images.Count {
		return fmt.Errorf("screenshot or image upload evidence count is inconsistent")
	}
	return nil
}

func validSummaryRule(rule summaryRule, siteCode, role string) bool {
	return rule.SiteCode == siteCode && rule.Role == role && rule.RevisionID != "" && len(rule.Fingerprint) == 64 &&
		rule.Accepted && len(rule.AcceptanceSHA) == 64
}

func summaryEvidenceBlock(err error) *BlockError {
	return &BlockError{
		Blockers:    []Blocker{{Code: "summary_evidence_incomplete", Message: err.Error()}},
		NextActions: []NextAction{{Action: "inspect_workflow_evidence", Description: "Do not report completion; inspect the missing or inconsistent immutable evidence and restart the affected workflow."}},
		ResumeState: map[string]any{"summary": map[string]any{"status": "incomplete"}},
	}
}
