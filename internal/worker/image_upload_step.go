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
	Recovered  bool                      `json:"recovered"`
}

type pendingImageUploadEvidence struct {
	Source screenshotArtifactInput   `json:"source"`
	Host   imagehosts.HostSnapshot   `json:"host"`
	Upload imagehosts.UploadEvidence `json:"upload"`
}

type imageUploadReconciliation struct {
	BlockerCode           string `json:"blocker_code"`
	AttemptID             string `json:"attempt_id"`
	Decision              string `json:"decision"`
	Confirmed             bool   `json:"confirmed"`
	EvidenceSHA256        string `json:"evidence_sha256"`
	ObservedAt            string `json:"observed_at"`
	PendingEvidenceSHA256 string `json:"pending_evidence_sha256"`
}

func (executor imageUploadExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil || executor.artifacts == nil || executor.catalog == nil {
		return nil, fmt.Errorf("image upload workflow dependencies are unavailable")
	}
	hostName, screenshots, pending, reconciliation, err := imageUploadInputs(execution.Step.InputSnapshot)
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
	recoveryConsumed := false
	for _, screenshot := range screenshots {
		if reused, ok := reusableImageReceipt(existing, screenshot, host); ok {
			receipts = append(receipts, reused)
			if reconciliation.Decision == "verified_uploaded" && pending != nil && pending.Source.SHA256 == screenshot.SHA256 {
				recoveryConsumed = true
			}
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
		image := imagehosts.Image{
			Filename: screenshot.Filename, MIMEType: screenshot.MIMEType,
			Bytes: imageBytes, SHA256: screenshot.SHA256,
		}
		if reconciliation.Decision == "verified_uploaded" && !recoveryConsumed {
			if pending == nil || pending.Source.SHA256 != screenshot.SHA256 {
				return nil, imageUploadReconciliationBlock("pending image evidence must be recovered before any further remote upload", hostName, pending, reconciliation)
			}
			receipt, err := executor.recoverImageReceipt(ctx, execution, screenshot, image, *pending, reconciliation)
			if err != nil {
				return nil, err
			}
			receipts = append(receipts, receipt)
			recoveryConsumed = true
			continue
		}
		upload, err := executor.provider.Upload(ctx, hostName, imagehosts.Image{
			Filename: screenshot.Filename, MIMEType: screenshot.MIMEType,
			Bytes: imageBytes, SHA256: screenshot.SHA256,
		}, execution.Actor)
		if err != nil {
			if errors.Is(err, imagehosts.ErrUploadOutcomeUnknown) {
				return nil, imageUploadOutcomeBlock(hostName, screenshot, host, upload, err)
			}
			return nil, imageHostBlock(err, hostName, "upload_screenshot")
		}
		if err := imagehosts.ValidateUploadEvidence(upload, image); err != nil || !uploadMatchesImageHost(upload, host) {
			if err == nil {
				err = errors.New("image host configuration changed while the screenshot was being uploaded")
			}
			return nil, imageUploadOutcomeBlock(hostName, screenshot, hostSnapshotFromUpload(upload), upload, err)
		}
		receipt := imageUploadReceipt{
			Index: screenshot.Index, Timestamp: screenshot.Timestamp,
			Source: screenshot, Host: host, Upload: upload,
		}
		receipt, err = executor.persistImageReceipt(ctx, execution, receipt)
		if err != nil {
			return nil, imageUploadOutcomeBlock(hostName, screenshot, host, upload, err)
		}
		receipts = append(receipts, receipt)
	}
	if reconciliation.Decision == "verified_uploaded" && !recoveryConsumed {
		return nil, imageUploadReconciliationBlock("the pending image evidence is not bound to any current screenshot", hostName, pending, reconciliation)
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

func (executor imageUploadExecutor) persistImageReceipt(
	ctx context.Context,
	execution Execution,
	receipt imageUploadReceipt,
) (imageUploadReceipt, error) {
	receiptBody, err := json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		return imageUploadReceipt{}, fmt.Errorf("serialize image upload receipt: %w", err)
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, fmt.Sprintf("image-upload-%02d.json", receipt.Index), bytes.NewReader(receiptBody))
	if err != nil {
		return imageUploadReceipt{}, fmt.Errorf("persist image upload receipt: %w", err)
	}
	recorded, err := executor.catalog.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "image_upload_receipt", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"index": receipt.Index, "source_artifact_id": receipt.Source.ArtifactID,
			"source_sha256": receipt.Source.SHA256, "image_host_id": receipt.Host.ID,
			"image_host_name": receipt.Host.Name, "image_host_adapter": receipt.Host.Adapter,
			"image_host_config_sha256": receipt.Host.ConfigSHA256,
			"image_host_updated_at":    receipt.Host.ConfigurationTime, "url": receipt.Upload.Result.URL,
			"viewer_url": receipt.Upload.Result.ViewerURL, "thumbnail_url": receipt.Upload.Result.ThumbnailURL,
			"remote_id": receipt.Upload.Result.RemoteID, "extension": receipt.Upload.Result.Extension,
			"recovered": receipt.Recovered,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return imageUploadReceipt{}, fmt.Errorf("register image upload receipt: %w", err)
	}
	receipt.ReceiptID, receipt.ReceiptSHA = recorded.ID, recorded.SHA256
	return receipt, nil
}

func (executor imageUploadExecutor) recoverImageReceipt(
	ctx context.Context,
	execution Execution,
	screenshot screenshotArtifactInput,
	image imagehosts.Image,
	pending pendingImageUploadEvidence,
	reconciliation imageUploadReconciliation,
) (imageUploadReceipt, error) {
	pendingSHA, err := pendingImageEvidenceHash(pending)
	evidenceDigest, evidenceErr := hex.DecodeString(reconciliation.EvidenceSHA256)
	observedAt, observedErr := time.Parse(time.RFC3339, reconciliation.ObservedAt)
	if err != nil || reconciliation.BlockerCode != "image_upload_outcome_unknown" || reconciliation.AttemptID == "" ||
		!reconciliation.Confirmed || evidenceErr != nil || len(evidenceDigest) != sha256.Size ||
		reconciliation.EvidenceSHA256 != strings.ToLower(reconciliation.EvidenceSHA256) || observedErr != nil || observedAt.IsZero() ||
		reconciliation.PendingEvidenceSHA256 != pendingSHA || pending.Source != screenshot ||
		imagehosts.ValidateUploadEvidence(pending.Upload, image) != nil || !uploadMatchesImageHost(pending.Upload, pending.Host) {
		return imageUploadReceipt{}, imageUploadReconciliationBlock("pending image evidence is invalid or is not bound to the current screenshot", pending.Host.Name, &pending, reconciliation)
	}
	receipt := imageUploadReceipt{
		Index: screenshot.Index, Timestamp: screenshot.Timestamp, Source: screenshot,
		Host: pending.Host, Upload: pending.Upload, Recovered: true,
	}
	receipt, err = executor.persistImageReceipt(ctx, execution, receipt)
	if err != nil {
		return imageUploadReceipt{}, imageUploadOutcomeBlock(pending.Host.Name, screenshot, pending.Host, pending.Upload, err)
	}
	return receipt, nil
}

func pendingImageEvidenceHash(pending pendingImageUploadEvidence) (string, error) {
	body, err := json.Marshal(pending)
	if err != nil {
		return "", err
	}
	var decoded any
	if err := json.Unmarshal(body, &decoded); err != nil {
		return "", err
	}
	canonical, err := json.Marshal(decoded)
	if err != nil {
		return "", err
	}
	return sha256Hex(canonical), nil
}

func imageUploadOutcomeBlock(
	name string,
	screenshot screenshotArtifactInput,
	host imagehosts.HostSnapshot,
	upload imagehosts.UploadEvidence,
	err error,
) *BlockError {
	var pending *pendingImageUploadEvidence
	parameters := map[string]any{
		"image_host_name": name, "source_sha256": screenshot.SHA256,
	}
	if upload.Result.URL != "" && upload.SourceSHA256 != "" {
		value := pendingImageUploadEvidence{Source: screenshot, Host: host, Upload: upload}
		pending = &value
		if digest, hashErr := pendingImageEvidenceHash(value); hashErr == nil {
			parameters["pending_evidence_sha256"] = digest
		}
	}
	imageState := map[string]any{"source_sha256": screenshot.SHA256}
	if pending != nil {
		imageState["pending_evidence"] = pending
	}
	return &BlockError{
		Blockers: []Blocker{{
			Code: "image_upload_outcome_unknown", Message: "image upload outcome requires reconciliation: " + err.Error(),
		}},
		NextActions: []NextAction{{
			Action: "reconcile_image_upload", Description: "Do not retry blindly. Verify whether this exact screenshot already exists remotely, then submit an attempt-bound reconciliation decision.",
			Parameters: parameters,
		}},
		ResumeState: map[string]any{
			"image_host": map[string]any{"name": name}, "image_upload": imageState,
		},
	}
}

func imageUploadReconciliationBlock(message, name string, pending *pendingImageUploadEvidence, reconciliation imageUploadReconciliation) *BlockError {
	imageState := map[string]any{}
	if pending != nil {
		imageState["pending_evidence"] = pending
	}
	return &BlockError{
		Blockers: []Blocker{{Code: "image_upload_reconciliation_invalid", Message: message}},
		NextActions: []NextAction{{
			Action: "review_image_upload_reconciliation", Description: "Keep the existing verified_uploaded decision and review the immutable pending evidence before retrying local receipt persistence.",
		}},
		ResumeState: map[string]any{
			"image_host": map[string]any{"name": name}, "image_upload": imageState, "reconciliation": reconciliation,
		},
	}
}

func hostSnapshotFromUpload(upload imagehosts.UploadEvidence) imagehosts.HostSnapshot {
	return imagehosts.HostSnapshot{
		ID: upload.ImageHostID, Name: upload.ImageHostName, Adapter: upload.Adapter,
		ConfigSHA256: upload.ConfigSHA256, ConfigurationTime: upload.ConfigurationTime,
	}
}

func imageUploadInputs(snapshotBody json.RawMessage) (string, []screenshotArtifactInput, *pendingImageUploadEvidence, imageUploadReconciliation, error) {
	type controls struct {
		ImageHost struct {
			Name string `json:"name"`
		} `json:"image_host"`
		ImageUpload struct {
			PendingEvidence *pendingImageUploadEvidence `json:"pending_evidence"`
		} `json:"image_upload"`
		Reconciliation imageUploadReconciliation `json:"reconciliation"`
	}
	var snapshot struct {
		JobInput      controls                   `json:"job_input"`
		ResumeState   controls                   `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return "", nil, nil, imageUploadReconciliation{}, fmt.Errorf("decode image upload snapshot: %w", err)
	}
	hostName := strings.TrimSpace(snapshot.JobInput.ImageHost.Name)
	if resumed := strings.TrimSpace(snapshot.ResumeState.ImageHost.Name); resumed != "" {
		hostName = resumed
	}
	if hostName == "" {
		hostName = "default"
	}
	if !integrationNamePattern.MatchString(hostName) {
		return "", nil, nil, imageUploadReconciliation{}, fmt.Errorf("image host name is invalid")
	}
	var generated struct {
		Generated bool                      `json:"generated"`
		Artifacts []screenshotArtifactInput `json:"artifacts"`
	}
	body, exists := snapshot.PreviousSteps["screenshots"]
	if !exists || json.Unmarshal(body, &generated) != nil || !generated.Generated || len(generated.Artifacts) == 0 {
		return "", nil, nil, imageUploadReconciliation{}, fmt.Errorf("screenshot artifact evidence is missing or incomplete")
	}
	return hostName, generated.Artifacts, snapshot.ResumeState.ImageUpload.PendingEvidence, snapshot.ResumeState.Reconciliation, nil
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
			HostName     string    `json:"image_host_name"`
			HostAdapter  string    `json:"image_host_adapter"`
			ConfigSHA    string    `json:"image_host_config_sha256"`
			ConfigTime   time.Time `json:"image_host_updated_at"`
			URL          string    `json:"url"`
			ViewerURL    string    `json:"viewer_url"`
			ThumbnailURL string    `json:"thumbnail_url"`
			RemoteID     string    `json:"remote_id"`
			Extension    string    `json:"extension"`
			Recovered    bool      `json:"recovered"`
		}
		if json.Unmarshal(artifact.Metadata, &metadata) != nil || !strings.EqualFold(metadata.SourceSHA256, screenshot.SHA256) ||
			metadata.HostID == "" || metadata.HostName == "" || len(metadata.ConfigSHA) != sha256.Size*2 ||
			metadata.ConfigTime.IsZero() || metadata.URL == "" {
			continue
		}
		if metadata.HostAdapter == "" && metadata.HostID == host.ID && metadata.HostName == host.Name {
			metadata.HostAdapter = host.Adapter
		}
		if metadata.HostAdapter == "" {
			continue
		}
		receiptHost := imagehosts.HostSnapshot{
			ID: metadata.HostID, Name: metadata.HostName, Adapter: metadata.HostAdapter,
			ConfigSHA256: strings.ToLower(metadata.ConfigSHA), ConfigurationTime: metadata.ConfigTime,
		}
		return imageUploadReceipt{
			Index: screenshot.Index, Timestamp: screenshot.Timestamp, Source: screenshot, Host: receiptHost,
			Upload: imagehosts.UploadEvidence{
				ImageHostID: receiptHost.ID, ImageHostName: receiptHost.Name, Adapter: receiptHost.Adapter,
				ConfigSHA256: receiptHost.ConfigSHA256, ConfigurationTime: receiptHost.ConfigurationTime,
				SourceFilename: screenshot.Filename, SourceMIMEType: screenshot.MIMEType,
				SourceSizeBytes: screenshot.SizeBytes, SourceSHA256: screenshot.SHA256,
				Result: imagehosts.UploadResult{
					URL: metadata.URL, ViewerURL: metadata.ViewerURL, ThumbnailURL: metadata.ThumbnailURL,
					RemoteID: metadata.RemoteID, Extension: metadata.Extension,
				},
			},
			ReceiptID: artifact.ID, ReceiptSHA: artifact.SHA256, Reused: true, Recovered: metadata.Recovered,
		}, true
	}
	return imageUploadReceipt{}, false
}

func uploadMatchesImageHost(upload imagehosts.UploadEvidence, host imagehosts.HostSnapshot) bool {
	return upload.ImageHostID == host.ID && upload.ImageHostName == host.Name && upload.Adapter == host.Adapter &&
		strings.EqualFold(upload.ConfigSHA256, host.ConfigSHA256) && upload.ConfigurationTime.Equal(host.ConfigurationTime)
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
		description = "Use ImgBB, PTPimg, Imgbox, or Pixhost, or implement the configured image-host adapter."
	}
	return &BlockError{
		Blockers:    []Blocker{{Code: code, Message: err.Error()}},
		NextActions: []NextAction{{Action: action, Description: description, Parameters: map[string]any{"image_host_name": name, "operation": operation}}},
		ResumeState: map[string]any{"image_host": map[string]any{"name": name}, "retry_step": operation},
	}
}
