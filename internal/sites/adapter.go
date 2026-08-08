package sites

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

type SourceInfo struct {
	Tracker           string    `json:"tracker"`
	TorrentID         string    `json:"torrent_id"`
	DetailsURL        string    `json:"details_url"`
	Name              string    `json:"name,omitempty"`
	IMDbID            string    `json:"imdb_id,omitempty"`
	TMDbID            string    `json:"tmdb_id,omitempty"`
	TMDbType          string    `json:"tmdb_type,omitempty"`
	DoubanID          string    `json:"douban_id,omitempty"`
	DoubanURL         string    `json:"douban_url,omitempty"`
	AniDBID           string    `json:"anidb_id,omitempty"`
	TorrentHash       string    `json:"torrent_hash,omitempty"`
	DescriptionLength int       `json:"description_length"`
	PromotionLabels   []string  `json:"promotion_labels"`
	Free              bool      `json:"free"`
	RetrievedAt       time.Time `json:"retrieved_at"`
	DescriptionHTML   string    `json:"-"`
}

type DownloadedTorrent struct {
	Bytes       []byte                 `json:"-"`
	Filename    string                 `json:"filename"`
	ContentType string                 `json:"content_type"`
	SizeBytes   int64                  `json:"size_bytes"`
	SHA256      string                 `json:"sha256"`
	Hashes      torrentmeta.InfoHashes `json:"hashes"`
}

type SourceAdapter interface {
	SiteCode() string
	Inspect(context.Context, SourceReference) (SourceInfo, error)
	Download(context.Context, SourceReference) (DownloadedTorrent, error)
}

type AdapterError struct {
	Code      string
	Message   string
	Temporary bool
	Cause     error
}

func (e *AdapterError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	return e.Code
}

func (e *AdapterError) Unwrap() error { return e.Cause }

func NewAdapterError(code, message string, temporary bool, cause error) error {
	return &AdapterError{Code: code, Message: message, Temporary: temporary, Cause: cause}
}

func ErrorDetails(err error) (code, message string, temporary bool) {
	var adapterError *AdapterError
	if errors.As(err, &adapterError) {
		return adapterError.Code, adapterError.Error(), adapterError.Temporary
	}
	return "site_adapter_failed", err.Error(), false
}

type Registry struct {
	adapters map[string]SourceAdapter
}

func NewRegistry(adapters ...SourceAdapter) (*Registry, error) {
	registry := &Registry{adapters: map[string]SourceAdapter{}}
	for _, adapter := range adapters {
		if err := registry.Register(adapter); err != nil {
			return nil, err
		}
	}
	return registry, nil
}

func (registry *Registry) Register(adapter SourceAdapter) error {
	if adapter == nil {
		return fmt.Errorf("site adapter is nil")
	}
	code := strings.ToUpper(strings.TrimSpace(adapter.SiteCode()))
	if code == "" {
		return fmt.Errorf("site adapter code is required")
	}
	if _, exists := registry.adapters[code]; exists {
		return fmt.Errorf("site adapter %s is already registered", code)
	}
	registry.adapters[code] = adapter
	return nil
}

func (registry *Registry) Inspect(ctx context.Context, reference SourceReference) (SourceInfo, error) {
	adapter, err := registry.adapter(reference.Tracker)
	if err != nil {
		return SourceInfo{}, err
	}
	return adapter.Inspect(ctx, reference)
}

func (registry *Registry) Download(ctx context.Context, reference SourceReference) (DownloadedTorrent, error) {
	adapter, err := registry.adapter(reference.Tracker)
	if err != nil {
		return DownloadedTorrent{}, err
	}
	return adapter.Download(ctx, reference)
}

func (registry *Registry) adapter(siteCode string) (SourceAdapter, error) {
	code := strings.ToUpper(strings.TrimSpace(siteCode))
	adapter, exists := registry.adapters[code]
	if !exists {
		return nil, NewAdapterError(
			"site_adapter_unavailable",
			fmt.Sprintf("source adapter for %s is not implemented", code),
			false, nil,
		)
	}
	return adapter, nil
}
