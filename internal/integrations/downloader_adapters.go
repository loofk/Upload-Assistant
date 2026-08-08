package integrations

import (
	"fmt"
	"slices"
	"strings"
)

var downloaderAdapterCatalog = []DownloaderAdapterCapability{
	{
		Adapter: "qbittorrent", DisplayName: "qBittorrent", RuntimeSupported: true,
		CredentialFields: []string{"api_key", "password", "username"},
		Operations:       DownloaderOperations{Probe: true, AddTorrent: true, Inspect: true, ListFiles: true, SetLimits: true, WaitComplete: true, Category: true, Tags: true, SkipChecking: true},
	},
	{
		Adapter: "transmission", DisplayName: "Transmission", RuntimeSupported: true,
		CredentialFields: []string{"password", "username"},
		Operations:       DownloaderOperations{Probe: true, AddTorrent: true, Inspect: true, ListFiles: true, SetLimits: true, WaitComplete: true, Category: true, Tags: true, SkipChecking: false},
	},
	{
		Adapter: "deluge", DisplayName: "Deluge", RuntimeSupported: true,
		CredentialFields: []string{"password"},
		Operations:       DownloaderOperations{Probe: true, AddTorrent: true, Inspect: true, ListFiles: true, SetLimits: true, WaitComplete: true, Category: false, Tags: false, SkipChecking: false},
		Constraints: []string{
			"endpoint must be the Deluge Web JSON-RPC service and the Web session must already be connected to a daemon",
			"authentication uses the Deluge Web password; native daemon RPC credentials are not accepted",
			"category and tags are unavailable because they require a separately versioned Label plugin contract; every add/workflow request must explicitly set apply_labels=false",
			"v2-only torrents and skip_checking are unsupported; seed_mode is never inferred or enabled",
		},
	},
	{
		Adapter: "rtorrent", DisplayName: "rTorrent", RuntimeSupported: true,
		CredentialFields: []string{"password", "username"},
		Operations:       DownloaderOperations{Probe: true, AddTorrent: true, Inspect: true, ListFiles: true, SetLimits: true, WaitComplete: true, Category: true, Tags: true, SkipChecking: false},
		Constraints: []string{
			"category and tags are stored as one comma-separated custom1 label",
			"per-torrent named throttles require non-zero effective global throttles and are verified after assignment",
			"v2-only torrents are unsupported because rTorrent requires a v1 infohash",
			"seeding time is conservatively measured from the current uninterrupted active window and may undercount prior sessions",
		},
	},
}

func DownloaderAdapterCapabilities() []DownloaderAdapterCapability {
	result := make([]DownloaderAdapterCapability, len(downloaderAdapterCatalog))
	for index, item := range downloaderAdapterCatalog {
		result[index] = item
		result[index].CredentialFields = append([]string(nil), item.CredentialFields...)
		result[index].Constraints = append([]string(nil), item.Constraints...)
	}
	return result
}

func DownloaderAdapterCapabilityFor(adapter string) (DownloaderAdapterCapability, bool) {
	adapter = strings.ToLower(strings.TrimSpace(adapter))
	for _, item := range downloaderAdapterCatalog {
		if item.Adapter == adapter {
			item.CredentialFields = append([]string(nil), item.CredentialFields...)
			item.Constraints = append([]string(nil), item.Constraints...)
			return item, true
		}
	}
	return DownloaderAdapterCapability{}, false
}

func attachDownloaderCapability(downloader *Downloader) {
	if capability, ok := DownloaderAdapterCapabilityFor(downloader.Adapter); ok {
		downloader.AdapterCapability = capability
	}
}

func validateDownloaderCredentialContract(capability DownloaderAdapterCapability, credentials map[string]string) error {
	for field := range credentials {
		if !slices.Contains(capability.CredentialFields, field) {
			return fmt.Errorf("%w: credential field %q is not supported by downloader adapter %q", ErrValidation, field, capability.Adapter)
		}
	}
	if slices.Contains(capability.CredentialFields, "username") {
		username, hasUsername := credentials["username"]
		password, hasPassword := credentials["password"]
		if hasUsername != hasPassword || (hasUsername && (strings.TrimSpace(username) == "" || password == "")) {
			return fmt.Errorf("%w: downloader adapter %q requires username and password together", ErrValidation, capability.Adapter)
		}
	}
	return nil
}

func validateDownloaderOptionContract(capability DownloaderAdapterCapability, options map[string]any) error {
	if category, ok := options["category"].(string); ok && strings.TrimSpace(category) != "" && !capability.Operations.Category {
		return fmt.Errorf("%w: downloader adapter %q does not support configured category defaults", ErrValidation, capability.Adapter)
	}
	if !capability.Operations.Tags {
		for _, field := range []string{"tag", "label"} {
			if value, ok := options[field].(string); ok && strings.TrimSpace(value) != "" {
				return fmt.Errorf("%w: downloader adapter %q does not support configured %s defaults", ErrValidation, capability.Adapter, field)
			}
		}
	}
	return nil
}
