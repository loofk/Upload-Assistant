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
		Adapter: "deluge", DisplayName: "Deluge", RuntimeSupported: false,
		CredentialFields:  []string{"password", "username"},
		UnavailableReason: "native Go Deluge runtime is not implemented yet; save this adapter disabled",
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
	username, hasUsername := credentials["username"]
	password, hasPassword := credentials["password"]
	if hasUsername != hasPassword || (hasUsername && (strings.TrimSpace(username) == "" || password == "")) {
		return fmt.Errorf("%w: downloader adapter %q requires username and password together", ErrValidation, capability.Adapter)
	}
	return nil
}
