package sites

import (
	"context"
	"fmt"
	"strings"
)

type TargetTorrentProfile struct {
	SiteCode             string   `json:"site_code"`
	Adapter              string   `json:"adapter"`
	AnnounceURL          string   `json:"-"`
	SourceTag            string   `json:"source_tag"`
	RequiredTopLevelKeys []string `json:"required_top_level_keys"`
}

type TargetTorrentAdapter interface {
	SiteCode() string
	TorrentProfile(context.Context) (TargetTorrentProfile, error)
}

type TargetTorrentRegistry struct {
	adapters map[string]TargetTorrentAdapter
}

func NewTargetTorrentRegistry(adapters ...TargetTorrentAdapter) (*TargetTorrentRegistry, error) {
	registry := &TargetTorrentRegistry{adapters: make(map[string]TargetTorrentAdapter)}
	for _, adapter := range adapters {
		if adapter == nil {
			return nil, fmt.Errorf("target torrent adapter is nil")
		}
		code := strings.ToUpper(strings.TrimSpace(adapter.SiteCode()))
		if code == "" {
			return nil, fmt.Errorf("target torrent adapter code is required")
		}
		if _, exists := registry.adapters[code]; exists {
			return nil, fmt.Errorf("target torrent adapter %s is already registered", code)
		}
		registry.adapters[code] = adapter
	}
	return registry, nil
}

func (registry *TargetTorrentRegistry) TorrentProfile(ctx context.Context, siteCode string) (TargetTorrentProfile, error) {
	code := strings.ToUpper(strings.TrimSpace(siteCode))
	adapter, exists := registry.adapters[code]
	if !exists {
		return TargetTorrentProfile{}, NewAdapterError(
			"target_torrent_adapter_unavailable",
			fmt.Sprintf("target torrent adapter for %s is not implemented", code), false, nil,
		)
	}
	profile, err := adapter.TorrentProfile(ctx)
	if err != nil {
		return TargetTorrentProfile{}, err
	}
	profile.SiteCode = strings.ToUpper(strings.TrimSpace(profile.SiteCode))
	if profile.SiteCode != code || strings.TrimSpace(profile.Adapter) == "" || strings.TrimSpace(profile.AnnounceURL) == "" ||
		strings.TrimSpace(profile.SourceTag) == "" || len(profile.RequiredTopLevelKeys) == 0 {
		return TargetTorrentProfile{}, NewAdapterError(
			"target_torrent_profile_invalid", "target torrent profile is incomplete or bound to another site", false, nil,
		)
	}
	return profile, nil
}
