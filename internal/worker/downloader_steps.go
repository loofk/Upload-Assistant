package worker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"regexp"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const maxWorkflowTorrentBytes = 40 << 20

type DownloaderProvider interface {
	Add(context.Context, string, []byte, qbittorrent.AddOptions, workflow.Actor) (downloaders.AddEvidence, error)
	Inspect(context.Context, string, string, workflow.Actor) (downloaders.TorrentEvidence, error)
	Files(context.Context, string, string, workflow.Actor) (downloaders.TorrentFilesEvidence, error)
}

type ArtifactReader interface {
	Open(string) (*os.File, error)
}

type WorkflowArtifactStore interface {
	ArtifactReader
	ArtifactWriter
}

func WithDownloader(provider DownloaderProvider, artifactStore WorkflowArtifactStore, allowedContentRoots ...string) Option {
	return func(runner *Runner) {
		if len(allowedContentRoots) == 0 {
			allowedContentRoots = []string{"/downloads"}
		}
		runner.executors["downloader_add"] = downloaderAddExecutor{provider: provider, artifacts: artifactStore}
		runner.executors["downloader_wait"] = downloaderWaitExecutor{provider: provider}
		runner.executors["content_resolve"] = contentResolveExecutor{
			provider: provider, artifacts: artifactStore, recorder: runner.runtime,
			allowedRoots: append([]string(nil), allowedContentRoots...),
		}
	}
}

type downloaderAddExecutor struct {
	provider  DownloaderProvider
	artifacts ArtifactReader
}

type downloaderWaitExecutor struct{ provider DownloaderProvider }

type downloaderControl struct {
	Name          string   `json:"name"`
	SavePath      string   `json:"save_path"`
	Category      string   `json:"category"`
	Tags          []string `json:"tags"`
	SkipChecking  *bool    `json:"skip_checking,omitempty"`
	Paused        *bool    `json:"paused,omitempty"`
	DownloadLimit int64    `json:"download_limit_bytes_per_second,omitempty"`
	UploadLimit   int64    `json:"upload_limit_bytes_per_second,omitempty"`
}

type retorrentRuntimeControls struct {
	Downloader downloaderControl `json:"downloader"`
}

type downloaderSnapshot struct {
	JobInput      retorrentRuntimeControls   `json:"job_input"`
	ResumeState   retorrentRuntimeControls   `json:"resume_state"`
	PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
}

type sourceTorrentStepOutput struct {
	StoragePath string `json:"storage_path"`
	SizeBytes   int64  `json:"size_bytes"`
	SHA256      string `json:"sha256"`
}

type sourceRuleStepOutput struct {
	Fingerprint string       `json:"fingerprint"`
	Limits      rules.Limits `json:"limits"`
}

func (executor downloaderAddExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil || executor.artifacts == nil {
		return nil, fmt.Errorf("downloader workflow dependencies are unavailable")
	}
	snapshot, control, sourceTorrent, sourceRule, err := parseDownloaderSnapshot(execution.Step.InputSnapshot)
	_ = snapshot
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	if err := validateDownloaderControl(control); err != nil {
		return nil, downloaderConfigurationBlock(err, control)
	}
	policyDownload, err := rules.ParseByteRate(sourceRule.Limits.Download)
	if err != nil {
		return nil, ruleLimitBlock("download", sourceRule, err)
	}
	policyUpload, err := rules.ParseByteRate(sourceRule.Limits.Upload)
	if err != nil {
		return nil, ruleLimitBlock("upload", sourceRule, err)
	}
	appliedDownload := strictestLimit(control.DownloadLimit, policyDownload)
	appliedUpload := strictestLimit(control.UploadLimit, policyUpload)
	metainfo, err := readArtifact(executor.artifacts, sourceTorrent)
	if err != nil {
		return nil, &BlockError{
			Blockers:    []Blocker{{Code: "source_torrent_artifact_unavailable", Message: err.Error()}},
			NextActions: []NextAction{{Action: "restart_source_torrent_step", Description: "Restore or regenerate the immutable source torrent artifact before resuming."}},
			ResumeState: map[string]any{"artifact": sourceTorrent},
		}
	}
	addOptions := qbittorrent.AddOptions{
		SavePath: control.SavePath, Category: control.Category, Tags: control.Tags,
		SkipChecking: boolValue(control.SkipChecking), Paused: boolValue(control.Paused),
		DownloadLimit: appliedDownload, UploadLimit: appliedUpload,
	}
	evidence, err := executor.provider.Add(ctx, control.Name, metainfo, addOptions, execution.Actor)
	if err != nil {
		return nil, downloaderBlock(err, control.Name, "add_source_torrent", map[string]any{
			"source_torrent_sha256": sourceTorrent.SHA256,
		})
	}
	torrentHash := evidence.Result.Hashes.V1SHA1
	if torrentHash == "" {
		torrentHash = evidence.Result.Hashes.V2SHA256
	}
	return mustJSON(map[string]any{
		"downloader_name": control.Name, "torrent_hash": torrentHash,
		"add_evidence": evidence,
		"limits": map[string]any{
			"requested_download": control.DownloadLimit, "requested_upload": control.UploadLimit,
			"policy_download": policyDownload, "policy_upload": policyUpload,
			"applied_download": appliedDownload, "applied_upload": appliedUpload,
			"rule_fingerprint": sourceRule.Fingerprint,
		},
		"options": map[string]any{
			"save_path": control.SavePath, "category": control.Category, "tags": control.Tags,
			"skip_checking": boolValue(control.SkipChecking), "paused": boolValue(control.Paused),
		},
	}), nil
}

func (executor downloaderWaitExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil {
		return nil, fmt.Errorf("downloader workflow dependency is unavailable")
	}
	var snapshot struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		return nil, invalidSnapshotBlock(fmt.Errorf("decode downloader wait snapshot: %w", err))
	}
	var added struct {
		DownloaderName string `json:"downloader_name"`
		TorrentHash    string `json:"torrent_hash"`
	}
	body, exists := snapshot.PreviousSteps["downloader_add"]
	if !exists || json.Unmarshal(body, &added) != nil || added.DownloaderName == "" || added.TorrentHash == "" {
		return nil, invalidSnapshotBlock(fmt.Errorf("downloader_add evidence is missing or incomplete"))
	}
	evidence, err := executor.provider.Inspect(ctx, added.DownloaderName, added.TorrentHash, execution.Actor)
	if err != nil {
		return nil, downloaderBlock(err, added.DownloaderName, "inspect_source_download", map[string]any{
			"torrent_hash": added.TorrentHash,
		})
	}
	torrent := evidence.Torrent
	complete := torrent.Progress >= 0.999999 || (torrent.TotalSize > 0 && torrent.AmountLeft == 0)
	if !complete {
		action := "resume_job_when_download_progresses"
		description := "Resume this job later to re-inspect qBittorrent without repeating completed prior steps."
		if strings.Contains(strings.ToLower(torrent.State), "paused") || strings.Contains(strings.ToLower(torrent.State), "stopped") {
			action = "resume_torrent_in_downloader"
			description = "Resume the torrent in qBittorrent, then resume this job."
		}
		return nil, &BlockError{
			Blockers: []Blocker{{Code: "source_download_incomplete", Message: "qBittorrent has not completed the source download"}},
			NextActions: []NextAction{{Action: action, Description: description, Parameters: map[string]any{
				"downloader_name": added.DownloaderName, "torrent_hash": torrent.Hash,
			}}},
			ResumeState: map[string]any{"downloader_wait": map[string]any{
				"downloader_name": added.DownloaderName, "torrent_hash": torrent.Hash,
				"state": torrent.State, "progress": torrent.Progress,
				"completed_bytes": torrent.Completed, "total_size": torrent.TotalSize,
			}},
		}
	}
	return mustJSON(map[string]any{
		"completed": true, "downloader_name": added.DownloaderName,
		"torrent_hash": torrent.Hash, "torrent": evidence,
	}), nil
}

func parseDownloaderSnapshot(raw json.RawMessage) (downloaderSnapshot, downloaderControl, sourceTorrentStepOutput, sourceRuleStepOutput, error) {
	var snapshot downloaderSnapshot
	if err := json.Unmarshal(raw, &snapshot); err != nil {
		return snapshot, downloaderControl{}, sourceTorrentStepOutput{}, sourceRuleStepOutput{}, fmt.Errorf("decode downloader step snapshot: %w", err)
	}
	control := snapshot.JobInput.Downloader
	mergeDownloaderControl(&control, snapshot.ResumeState.Downloader)
	if control.Name == "" {
		control.Name = "default"
	}
	if control.SavePath == "" {
		control.SavePath = "/downloads"
	}
	if control.Category == "" {
		control.Category = "retorrent"
	}
	var sourceTorrent sourceTorrentStepOutput
	if body, exists := snapshot.PreviousSteps["source_torrent"]; !exists || json.Unmarshal(body, &sourceTorrent) != nil || sourceTorrent.StoragePath == "" || sourceTorrent.SHA256 == "" {
		return snapshot, control, sourceTorrent, sourceRuleStepOutput{}, fmt.Errorf("source_torrent artifact evidence is missing or incomplete")
	}
	var sourceRule sourceRuleStepOutput
	if body, exists := snapshot.PreviousSteps["source_rules"]; !exists || json.Unmarshal(body, &sourceRule) != nil || sourceRule.Fingerprint == "" {
		return snapshot, control, sourceTorrent, sourceRule, fmt.Errorf("source_rules gate evidence is missing or incomplete")
	}
	return snapshot, control, sourceTorrent, sourceRule, nil
}

func mergeDownloaderControl(target *downloaderControl, override downloaderControl) {
	if override.Name != "" {
		target.Name = override.Name
	}
	if override.SavePath != "" {
		target.SavePath = override.SavePath
	}
	if override.Category != "" {
		target.Category = override.Category
	}
	if override.Tags != nil {
		target.Tags = override.Tags
	}
	if override.SkipChecking != nil {
		target.SkipChecking = override.SkipChecking
	}
	if override.Paused != nil {
		target.Paused = override.Paused
	}
	if override.DownloadLimit != 0 {
		target.DownloadLimit = override.DownloadLimit
	}
	if override.UploadLimit != 0 {
		target.UploadLimit = override.UploadLimit
	}
}

var integrationNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

func validateDownloaderControl(control downloaderControl) error {
	if !integrationNamePattern.MatchString(control.Name) {
		return fmt.Errorf("downloader name is invalid")
	}
	if !strings.HasPrefix(control.SavePath, "/") || path.Clean(control.SavePath) != control.SavePath {
		return fmt.Errorf("downloader save_path must be a normalized absolute Linux path")
	}
	if len(control.Category) > 100 || strings.ContainsAny(control.Category, "\r\n") {
		return fmt.Errorf("downloader category is invalid")
	}
	if len(control.Tags) > 32 {
		return fmt.Errorf("downloader tags must not exceed 32 entries")
	}
	for _, tag := range control.Tags {
		if strings.TrimSpace(tag) == "" || len(tag) > 100 || strings.ContainsAny(tag, ",\r\n") {
			return fmt.Errorf("downloader tag %q is invalid", tag)
		}
	}
	if control.DownloadLimit < 0 || control.UploadLimit < 0 {
		return fmt.Errorf("downloader limits must not be negative")
	}
	return nil
}

func readArtifact(reader ArtifactReader, evidence sourceTorrentStepOutput) ([]byte, error) {
	file, err := reader.Open(evidence.StoragePath)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, maxWorkflowTorrentBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read source torrent artifact: %w", err)
	}
	if len(body) > maxWorkflowTorrentBytes {
		return nil, fmt.Errorf("source torrent artifact exceeds %d bytes", maxWorkflowTorrentBytes)
	}
	if evidence.SizeBytes > 0 && int64(len(body)) != evidence.SizeBytes {
		return nil, fmt.Errorf("source torrent artifact size does not match recorded evidence")
	}
	digest := sha256.Sum256(body)
	if !strings.EqualFold(hex.EncodeToString(digest[:]), evidence.SHA256) {
		return nil, fmt.Errorf("source torrent artifact hash does not match recorded evidence")
	}
	return body, nil
}

func strictestLimit(requested, policy int64) int64 {
	if policy > 0 && (requested == 0 || policy < requested) {
		return policy
	}
	return requested
}

func boolValue(value *bool) bool { return value != nil && *value }

func downloaderConfigurationBlock(err error, control downloaderControl) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: "downloader_configuration_invalid", Message: err.Error()}},
		NextActions: []NextAction{{Action: "provide_downloader_parameters", Description: "Supply valid downloader parameters in resume_state.", Parameters: map[string]any{
			"downloader": map[string]any{"name": control.Name, "save_path": control.SavePath},
		}}},
		ResumeState: map[string]any{"downloader": control},
	}
}

func ruleLimitBlock(kind string, rule sourceRuleStepOutput, err error) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: "rule_limit_unparseable", Message: err.Error()}},
		NextActions: []NextAction{{Action: "review_rule_limit", Description: "Create, approve, and activate a corrected rule revision before resuming.", Parameters: map[string]any{
			"kind": kind, "fingerprint": rule.Fingerprint,
		}}},
		ResumeState: map[string]any{"active_rule_fingerprint": rule.Fingerprint},
	}
}

func downloaderBlock(err error, name, operation string, evidence map[string]any) *BlockError {
	code := "downloader_request_failed"
	action := "retry_step"
	description := "Verify downloader availability, then resume this step."
	switch {
	case errors.Is(err, integrations.ErrNotFound):
		code, action = "downloader_configuration_required", "configure_downloader"
		description = "Configure and enable the named downloader before resuming."
	case errors.Is(err, integrations.ErrValidation):
		code, action = "downloader_configuration_invalid", "configure_downloader"
		description = "Correct and enable the downloader configuration before resuming."
	case errors.Is(err, downloaders.ErrAdapterUnavailable):
		code, action = "downloader_adapter_unavailable", "install_downloader_adapter"
		description = "Use a supported downloader adapter or implement the configured adapter."
	case errors.Is(err, qbittorrent.ErrUnauthorized):
		code, action = "downloader_authentication_failed", "configure_downloader_credentials"
		description = "Refresh encrypted qBittorrent credentials before resuming."
	case errors.Is(err, qbittorrent.ErrNotFound):
		code, action = "downloader_torrent_not_observed", "retry_step"
		description = "Wait for qBittorrent to observe the torrent, then resume this step."
	}
	parameters := map[string]any{"downloader_name": name, "operation": operation}
	for key, value := range evidence {
		parameters[key] = value
	}
	return &BlockError{
		Blockers:    []Blocker{{Code: code, Message: err.Error()}},
		NextActions: []NextAction{{Action: action, Description: description, Parameters: parameters}},
		ResumeState: map[string]any{"downloader": map[string]any{"name": name}, "retry_step": operation},
	}
}
