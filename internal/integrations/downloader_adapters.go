package integrations

import "strings"

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
		Adapter: "rtorrent", DisplayName: "rTorrent", RuntimeSupported: false,
		CredentialFields:  []string{"password", "username"},
		UnavailableReason: "native Go rTorrent runtime is not implemented yet; save this adapter disabled",
	},
}

func DownloaderAdapterCapabilities() []DownloaderAdapterCapability {
	result := make([]DownloaderAdapterCapability, len(downloaderAdapterCatalog))
	for index, item := range downloaderAdapterCatalog {
		result[index] = item
		result[index].CredentialFields = append([]string(nil), item.CredentialFields...)
	}
	return result
}

func DownloaderAdapterCapabilityFor(adapter string) (DownloaderAdapterCapability, bool) {
	adapter = strings.ToLower(strings.TrimSpace(adapter))
	for _, item := range downloaderAdapterCatalog {
		if item.Adapter == adapter {
			item.CredentialFields = append([]string(nil), item.CredentialFields...)
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
