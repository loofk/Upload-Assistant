package sites

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/rules"
)

type TargetArtifactEvidence struct {
	ArtifactID  string `json:"artifact_id,omitempty"`
	StoragePath string `json:"storage_path,omitempty"`
	SHA256      string `json:"sha256,omitempty"`
	SizeBytes   int64  `json:"size_bytes,omitempty"`
}

type TargetContentEvidence struct {
	LocalRoot         string `json:"local_root"`
	FileCount         int    `json:"file_count"`
	TotalSizeBytes    int64  `json:"total_size_bytes"`
	ManifestID        string `json:"manifest_artifact_id"`
	ManifestSHA256    string `json:"manifest_sha256"`
	DownloaderName    string `json:"downloader_name,omitempty"`
	SourceTorrentHash string `json:"source_torrent_hash,omitempty"`
}

type TargetMediaEvidence struct {
	Kind     string                 `json:"kind"`
	Tool     string                 `json:"tool"`
	Version  string                 `json:"version"`
	Format   string                 `json:"format"`
	Document string                 `json:"document"`
	Artifact TargetArtifactEvidence `json:"artifact"`
}

type TargetScreenshotEvidence struct {
	Index             int    `json:"index"`
	SourceSHA256      string `json:"source_sha256"`
	ReceiptArtifactID string `json:"receipt_artifact_id"`
	ReceiptSHA256     string `json:"receipt_sha256"`
	URL               string `json:"url"`
	ViewerURL         string `json:"viewer_url,omitempty"`
}

type TargetPackageMaterial struct {
	Target                     string                     `json:"target"`
	Source                     SourceInfo                 `json:"source"`
	Title                      string                     `json:"title"`
	Links                      map[string]string          `json:"links"`
	MetadataDescription        string                     `json:"metadata_description,omitempty"`
	MetadataEnrichmentRequired bool                       `json:"metadata_enrichment_required,omitempty"`
	SourceDescription          string                     `json:"source_description,omitempty"`
	Content                    TargetContentEvidence      `json:"content"`
	Media                      TargetMediaEvidence        `json:"media"`
	Screenshots                []TargetScreenshotEvidence `json:"screenshots"`
	TargetRuleRevisionID       string                     `json:"target_rule_revision_id"`
	TargetRuleFingerprint      string                     `json:"target_rule_fingerprint"`
	Naming                     rules.Naming               `json:"naming,omitempty"`
	Advisories                 []rules.Advisory           `json:"advisories,omitempty"`
	Evidence                   map[string]any             `json:"evidence"`
	Options                    json.RawMessage            `json:"options"`
}

type TargetDecision struct {
	Field      string `json:"field"`
	Value      any    `json:"value"`
	Derivation string `json:"derivation"`
	Evidence   string `json:"evidence,omitempty"`
}

type PreparedTargetPackage struct {
	SchemaVersion        int                   `json:"schema_version"`
	Target               string                `json:"target"`
	Adapter              string                `json:"adapter"`
	Source               SourceInfo            `json:"source"`
	MetadataLinks        map[string]string     `json:"metadata_links"`
	FormFields           map[string]any        `json:"form_fields"`
	Description          string                `json:"description"`
	MediaInfo            json.RawMessage       `json:"mediainfo"`
	Content              TargetContentEvidence `json:"content"`
	Evidence             map[string]any        `json:"evidence"`
	Decisions            []TargetDecision      `json:"decisions"`
	Warnings             []string              `json:"warnings"`
	NamingProfiles       []rules.NamingProfile `json:"naming_profiles,omitempty"`
	ManualReviewRequired bool                  `json:"manual_review_required"`
	GeneratedAt          time.Time             `json:"generated_at"`
}

type PackageRequirement struct {
	Code       string         `json:"code"`
	Field      string         `json:"field"`
	Message    string         `json:"message"`
	Parameters map[string]any `json:"parameters,omitempty"`
}

type PackageRequirementsError struct {
	Requirements []PackageRequirement
}

func (e *PackageRequirementsError) Error() string {
	if len(e.Requirements) == 0 {
		return "target package requires additional input"
	}
	return e.Requirements[0].Message
}

type TargetPackageAdapter interface {
	SiteCode() string
	PreparePackage(context.Context, TargetPackageMaterial) (PreparedTargetPackage, error)
}

type TargetPackageRegistry struct {
	adapters map[string]TargetPackageAdapter
}

func NewTargetPackageRegistry(adapters ...TargetPackageAdapter) (*TargetPackageRegistry, error) {
	registry := &TargetPackageRegistry{adapters: map[string]TargetPackageAdapter{}}
	for _, adapter := range adapters {
		if adapter == nil {
			return nil, fmt.Errorf("target package adapter is nil")
		}
		code := strings.ToUpper(strings.TrimSpace(adapter.SiteCode()))
		if code == "" {
			return nil, fmt.Errorf("target package adapter code is required")
		}
		if _, exists := registry.adapters[code]; exists {
			return nil, fmt.Errorf("target package adapter %s is already registered", code)
		}
		registry.adapters[code] = adapter
	}
	return registry, nil
}

func (registry *TargetPackageRegistry) PreparePackage(ctx context.Context, material TargetPackageMaterial) (PreparedTargetPackage, error) {
	code := strings.ToUpper(strings.TrimSpace(material.Target))
	adapter, exists := registry.adapters[code]
	if !exists {
		return PreparedTargetPackage{}, NewAdapterError(
			"target_package_adapter_unavailable",
			fmt.Sprintf("target package adapter for %s is not implemented", code), false, nil,
		)
	}
	material.Target = code
	return adapter.PreparePackage(ctx, material)
}
