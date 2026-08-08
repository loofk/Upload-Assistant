package sites

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

// TargetTorrentDownloadRequest binds recovery of the tracker-issued torrent to
// the immutable upload result and the exact payload that was submitted.
type TargetTorrentDownloadRequest struct {
	JobID                    string `json:"-"`
	AttemptID                string `json:"-"`
	TorrentID                string `json:"torrent_id"`
	UploadReceiptSHA256      string `json:"upload_receipt_sha256"`
	SubmittedTorrentSHA256   string `json:"submitted_torrent_sha256"`
	ContentFingerprintSHA256 string `json:"content_fingerprint_sha256"`
}

// TargetTorrentDownloadEvidence deliberately omits both the signed download
// URL and the personalized announce URL. Their hashes remain auditable without
// exposing tracker credentials.
type TargetTorrentDownloadEvidence struct {
	SiteCode                string                 `json:"site_code"`
	Adapter                 string                 `json:"adapter"`
	ConfigurationSHA256     string                 `json:"configuration_sha256"`
	TorrentID               string                 `json:"torrent_id"`
	Filename                string                 `json:"filename"`
	ContentType             string                 `json:"content_type"`
	SizeBytes               int64                  `json:"size_bytes"`
	SHA256                  string                 `json:"sha256"`
	Hashes                  torrentmeta.InfoHashes `json:"hashes"`
	ContentFingerprint      string                 `json:"content_fingerprint_sha256"`
	AnnounceSHA256          string                 `json:"announce_sha256"`
	TokenResponseSHA256     string                 `json:"token_response_sha256"`
	SignedDownloadURLSHA256 string                 `json:"signed_download_url_sha256"`
	DownloadedAt            time.Time              `json:"downloaded_at"`
}

type DownloadedTargetTorrent struct {
	Bytes    []byte                        `json:"-"`
	Evidence TargetTorrentDownloadEvidence `json:"evidence"`
}

type TargetTorrentDownloadAdapter interface {
	SiteCode() string
	DownloadUploadedTorrent(context.Context, TargetTorrentDownloadRequest, workflow.Actor) (DownloadedTargetTorrent, error)
}

type TargetTorrentDownloadRegistry struct {
	adapters map[string]TargetTorrentDownloadAdapter
}

func NewTargetTorrentDownloadRegistry(adapters ...TargetTorrentDownloadAdapter) (*TargetTorrentDownloadRegistry, error) {
	registry := &TargetTorrentDownloadRegistry{adapters: make(map[string]TargetTorrentDownloadAdapter)}
	for _, adapter := range adapters {
		if adapter == nil {
			return nil, fmt.Errorf("target torrent download adapter is nil")
		}
		code := strings.ToUpper(strings.TrimSpace(adapter.SiteCode()))
		if code == "" {
			return nil, fmt.Errorf("target torrent download adapter code is required")
		}
		if _, exists := registry.adapters[code]; exists {
			return nil, fmt.Errorf("target torrent download adapter %s is already registered", code)
		}
		registry.adapters[code] = adapter
	}
	return registry, nil
}

func (registry *TargetTorrentDownloadRegistry) DownloadUploadedTorrent(ctx context.Context, siteCode string, request TargetTorrentDownloadRequest, actor workflow.Actor) (DownloadedTargetTorrent, error) {
	code := strings.ToUpper(strings.TrimSpace(siteCode))
	adapter, exists := registry.adapters[code]
	if !exists {
		return DownloadedTargetTorrent{}, NewAdapterError(
			"target_torrent_download_adapter_unavailable",
			fmt.Sprintf("target torrent download adapter for %s is not implemented", code),
			false, nil,
		)
	}
	return adapter.DownloadUploadedTorrent(ctx, request, actor)
}
