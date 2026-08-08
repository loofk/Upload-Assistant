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
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/imagehosts"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type ImageHostProvider interface {
	Snapshot(context.Context, string) (imagehosts.HostSnapshot, error)
	Upload(context.Context, string, imagehosts.Image, workflow.Actor) (imagehosts.UploadEvidence, error)
}

type ArtifactCatalog interface {
	ArtifactRecorder
	ListArtifacts(context.Context, string) ([]workflow.Artifact, error)
}

func WithImageHosts(provider ImageHostProvider, artifactStore WorkflowArtifactStore) Option {
	return func(runner *Runner) {
		runner.executors["image_upload"] = imageUploadExecutor{
			provider: provider, artifacts: artifactStore, catalog: runner.runtime,
		}
	}
}

type imageUploadExecutor struct {
	provider  ImageHostProvider
	artifacts WorkflowArtifactStore
	catalog   ArtifactCatalog
}

type screenshotArtifactInput struct {
	Index       int     `json:"index"`
	Timestamp   float64 `json:"timestamp_seconds"`
	ArtifactID  string  `json:"artifact_id"`
	Filename    string  `json:"filename"`
	MIMEType    string  `json:"mime_type"`
	SizeBytes   int64   `json:"size_bytes"`
	SHA256      string  `json:"sha256"`
	StoragePath string  `json:"storage_path"`
}

type imageUploadReceipt struct {
	Index      int                       `json:"index"`
	Timestamp  float64                   `json:"timestamp_seconds"`
	Source     screenshotArtifactInput   `json:"source"`
	Host       imagehosts.HostSnapshot   `json:"host"`
	Upload     imagehosts.UploadEvidence `json:"upload"`
	ReceiptID  string                    `json:"receipt_artifact_id,omitempty"`
	ReceiptSHA string                    `json:"receipt_sha256,omitempty"`
	Reused     bool                      `json:"reused"`
}

func (executor imageUploadExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil || executor.artifacts == nil || executor.catalog == nil {
		return nil, fmt.Errorf("image upload workflow dependencies are unavailable")
	}
	hostName, screenshots, err := imageUploadInputs(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	host, err := executor.provider.Snapshot(ctx, hostName)
	if err != nil {
		return nil, imageHostBlock(err, hostName, "resolve_image_host")
	}
	existing, err := executor.catalog.ListArtifacts(ctx, execution.Job.ID)
	if err != nil {
		return nil, fmt.Errorf("list prior image upload receipts: %w", err)
	}
	receipts := make([]imageUploadReceipt, 0, len(screenshots))
	for _, screenshot := range screenshots {
		if reused, ok := reusableImageReceipt(existing, screenshot, host); ok {
			receipts = append(receipts, reused)
			continue
		}
		imageBytes, err := readScreenshotArtifact(executor.artifacts, screenshot)
		if err != nil {
			return nil, &BlockError{
				Blockers:    []Blocker{{Code: "screenshot_artifact_unavailable", Message: err.Error()}},
				NextActions: []NextAction{{Action: "regenerate_screenshots", Description: "Restore or regenerate screenshot artifacts before retrying image upload."}},
				ResumeState: map[string]any{"image_upload": map[string]any{"image_host": hostName, "source_sha256": screenshot.SHA256}},
			}
		}
		upload, err := executor.provider.Upload(ctx, hostName, imagehosts.Image{
			Filename: screenshot.Filename, MIMEType: screenshot.MIMEType,
			Bytes: imageBytes, SHA256: screenshot.SHA256,
		}, execution.Actor)
		if err != nil {
			return nil, imageHostBlock(err, hostName, "upload_screenshot")
		}
		if !uploadMatchesImageHost(upload, host) {
			return nil, imageHostConfigurationChangedBlock(hostName)
		}
		receipt := imageUploadReceipt{
			Index: screenshot.Index, Timestamp: screenshot.Timestamp,
			Source: screenshot, Host: host, Upload: upload,
		}
		receiptBody, err := json.MarshalIndent(receipt, "", "  ")
		if err != nil {
			return nil, fmt.Errorf("serialize image upload receipt: %w", err)
		}
		file, err := executor.artifacts.Write(ctx, artifacts.Scope{
			JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		}, fmt.Sprintf("image-upload-%02d.json", screenshot.Index), bytes.NewReader(receiptBody))
		if err != nil {
			return nil, fmt.Errorf("persist image upload receipt: %w", err)
		}
		recorded, err := executor.catalog.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
			JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
			Kind: "image_upload_receipt", StoragePath: file.RelativePath, Filename: file.Filename,
			MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
			Metadata: mustJSON(map[string]any{
				"index": screenshot.Index, "source_artifact_id": screenshot.ArtifactID,
				"source_sha256": screenshot.SHA256, "image_host_id": host.ID,
				"image_host_name": host.Name, "image_host_config_sha256": host.ConfigSHA256,
				"image_host_updated_at": host.ConfigurationTime, "url": upload.Result.URL,
				"viewer_url": upload.Result.ViewerURL, "thumbnail_url": upload.Result.ThumbnailURL,
			}),
			Retention: artifactRetention, Actor: execution.Actor,
		})
		if err != nil {
			return nil, fmt.Errorf("register image upload receipt: %w", err)
		}
		receipt.ReceiptID, receipt.ReceiptSHA = recorded.ID, recorded.SHA256
		receipts = append(receipts, receipt)
	}
	urls := make([]string, 0, len(receipts))
	bbcode := make([]string, 0, len(receipts))
	for _, receipt := range receipts {
		urls = append(urls, receipt.Upload.Result.URL)
		bbcode = append(bbcode, "[img]"+receipt.Upload.Result.URL+"[/img]")
	}
	return mustJSON(map[string]any{
		"uploaded": true, "image_host": host, "image_count": len(receipts),
		"receipts": receipts, "urls": urls, "bbcode": strings.Join(bbcode, "\n"),
	}), nil
}

func imageUploadInputs(snapshotBody json.RawMessage) (string, []screenshotArtifactInput, error) {
	type controls struct {
		ImageHost struct {
			Name string `json:"name"`
		} `json:"image_host"`
	}
	var snapshot struct {
		JobInput      controls                   `json:"job_input"`
		ResumeState   controls                   `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return "", nil, fmt.Errorf("decode image upload snapshot: %w", err)
	}
	hostName := strings.TrimSpace(snapshot.JobInput.ImageHost.Name)
	if resumed := strings.TrimSpace(snapshot.ResumeState.ImageHost.Name); resumed != "" {
		hostName = resumed
	}
	if hostName == "" {
		hostName = "default"
	}
	if !integrationNamePattern.MatchString(hostName) {
		return "", nil, fmt.Errorf("image host name is invalid")
	}
	var generated struct {
		Generated bool                      `json:"generated"`
		Artifacts []screenshotArtifactInput `json:"artifacts"`
	}
	body, exists := snapshot.PreviousSteps["screenshots"]
	if !exists || json.Unmarshal(body, &generated) != nil || !generated.Generated || len(generated.Artifacts) == 0 {
		return "", nil, fmt.Errorf("screenshot artifact evidence is missing or incomplete")
	}
	return hostName, generated.Artifacts, nil
}

func readScreenshotArtifact(store ArtifactReader, screenshot screenshotArtifactInput) ([]byte, error) {
	file, err := store.Open(screenshot.StoragePath)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, (32<<20)+1))
	if err != nil || len(body) > 32<<20 {
		return nil, fmt.Errorf("screenshot artifact is unreadable or too large")
	}
	if int64(len(body)) != screenshot.SizeBytes {
		return nil, fmt.Errorf("screenshot artifact size does not match evidence")
	}
	digest := sha256.Sum256(body)
	if !strings.EqualFold(hex.EncodeToString(digest[:]), screenshot.SHA256) {
		return nil, fmt.Errorf("screenshot artifact hash does not match evidence")
	}
	return body, nil
}

func reusableImageReceipt(existing []workflow.Artifact, screenshot screenshotArtifactInput, host imagehosts.HostSnapshot) (imageUploadReceipt, bool) {
	for _, artifact := range existing {
		if artifact.Kind != "image_upload_receipt" {
			continue
		}
		var metadata struct {
			SourceSHA256 string    `json:"source_sha256"`
			HostID       string    `json:"image_host_id"`
			ConfigSHA    string    `json:"image_host_config_sha256"`
			ConfigTime   time.Time `json:"image_host_updated_at"`
			URL          string    `json:"url"`
			ViewerURL    string    `json:"viewer_url"`
			ThumbnailURL string    `json:"thumbnail_url"`
		}
		if json.Unmarshal(artifact.Metadata, &metadata) != nil || !strings.EqualFold(metadata.SourceSHA256, screenshot.SHA256) ||
			metadata.HostID != host.ID || !strings.EqualFold(metadata.ConfigSHA, host.ConfigSHA256) ||
			!metadata.ConfigTime.Equal(host.ConfigurationTime) || metadata.URL == "" {
			continue
		}
		return imageUploadReceipt{
			Index: screenshot.Index, Timestamp: screenshot.Timestamp, Source: screenshot, Host: host,
			Upload: imagehosts.UploadEvidence{
				ImageHostID: host.ID, ImageHostName: host.Name, Adapter: host.Adapter,
				ConfigSHA256: host.ConfigSHA256, ConfigurationTime: host.ConfigurationTime,
				SourceFilename: screenshot.Filename, SourceMIMEType: screenshot.MIMEType,
				SourceSizeBytes: screenshot.SizeBytes, SourceSHA256: screenshot.SHA256,
				Result: imagehosts.UploadResult{URL: metadata.URL, ViewerURL: metadata.ViewerURL, ThumbnailURL: metadata.ThumbnailURL},
			},
			ReceiptID: artifact.ID, ReceiptSHA: artifact.SHA256, Reused: true,
		}, true
	}
	return imageUploadReceipt{}, false
}

func uploadMatchesImageHost(upload imagehosts.UploadEvidence, host imagehosts.HostSnapshot) bool {
	return upload.ImageHostID == host.ID && upload.ImageHostName == host.Name && upload.Adapter == host.Adapter &&
		strings.EqualFold(upload.ConfigSHA256, host.ConfigSHA256) && upload.ConfigurationTime.Equal(host.ConfigurationTime)
}

func imageHostConfigurationChangedBlock(name string) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{
			Code:    "image_host_configuration_changed",
			Message: "image host configuration changed while the screenshot was being uploaded",
		}},
		NextActions: []NextAction{{
			Action:      "retry_image_upload",
			Description: "Retry the resumable step so every receipt is bound to one current image-host configuration revision.",
			Parameters:  map[string]any{"image_host_name": name},
		}},
		ResumeState: map[string]any{"image_host": map[string]any{"name": name}, "retry_step": "upload_screenshot"},
	}
}

func imageHostBlock(err error, name, operation string) *BlockError {
	code, action := "image_host_request_failed", "retry_image_upload"
	description := "Verify image-host availability and retry this resumable step."
	switch {
	case errors.Is(err, integrations.ErrNotFound):
		code, action = "image_host_configuration_required", "configure_image_host"
		description = "Configure and enable the named image host before resuming."
	case errors.Is(err, integrations.ErrValidation):
		code, action = "image_host_configuration_invalid", "configure_image_host"
		description = "Correct the image host configuration before resuming."
	case errors.Is(err, imagehosts.ErrAdapterUnavailable):
		code, action = "image_host_adapter_unavailable", "install_image_host_adapter"
		description = "Use imgbb/PTPimg or implement the configured image host adapter."
	}
	return &BlockError{
		Blockers:    []Blocker{{Code: code, Message: err.Error()}},
		NextActions: []NextAction{{Action: action, Description: description, Parameters: map[string]any{"image_host_name": name, "operation": operation}}},
		ResumeState: map[string]any{"image_host": map[string]any{"name": name}, "retry_step": operation},
	}
}
