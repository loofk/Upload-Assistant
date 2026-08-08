package sites

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type TargetUploadRequest struct {
	JobID                    string                `json:"-"`
	AttemptID                string                `json:"-"`
	Confirmed                bool                  `json:"confirmed"`
	Package                  PreparedTargetPackage `json:"-"`
	Torrent                  []byte                `json:"-"`
	PackageSHA256            string                `json:"package_sha256"`
	TorrentSHA256            string                `json:"torrent_sha256"`
	ContentFingerprintSHA256 string                `json:"content_fingerprint_sha256"`
	RuleFingerprint          string                `json:"rule_fingerprint"`
	DuplicateCheckSHA256     string                `json:"duplicate_check_sha256"`
}

type TargetUploadEvidence struct {
	SiteCode            string    `json:"site_code"`
	Adapter             string    `json:"adapter"`
	ConfigurationSHA256 string    `json:"configuration_sha256"`
	TorrentID           string    `json:"torrent_id"`
	DetailsURL          string    `json:"details_url"`
	ResponseSHA256      string    `json:"response_sha256"`
	SubmittedAt         time.Time `json:"submitted_at"`
}

type TargetUploadAdapter interface {
	SiteCode() string
	Upload(context.Context, TargetUploadRequest, workflow.Actor) (TargetUploadEvidence, error)
}

type TargetUploadRegistry struct {
	adapters map[string]TargetUploadAdapter
}

func NewTargetUploadRegistry(adapters ...TargetUploadAdapter) (*TargetUploadRegistry, error) {
	registry := &TargetUploadRegistry{adapters: make(map[string]TargetUploadAdapter)}
	for _, adapter := range adapters {
		if adapter == nil {
			return nil, fmt.Errorf("target upload adapter is nil")
		}
		code := strings.ToUpper(strings.TrimSpace(adapter.SiteCode()))
		if code == "" {
			return nil, fmt.Errorf("target upload adapter code is required")
		}
		if _, exists := registry.adapters[code]; exists {
			return nil, fmt.Errorf("target upload adapter %s is already registered", code)
		}
		registry.adapters[code] = adapter
	}
	return registry, nil
}

func (registry *TargetUploadRegistry) Upload(ctx context.Context, siteCode string, request TargetUploadRequest, actor workflow.Actor) (TargetUploadEvidence, error) {
	code := strings.ToUpper(strings.TrimSpace(siteCode))
	adapter, exists := registry.adapters[code]
	if !exists {
		return TargetUploadEvidence{}, NewAdapterError(
			"target_upload_adapter_unavailable", fmt.Sprintf("target upload adapter for %s is not implemented", code), false, nil,
		)
	}
	return adapter.Upload(ctx, request, actor)
}
