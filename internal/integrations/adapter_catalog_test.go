package integrations

import (
	"os"
	"slices"
	"strings"
	"testing"
)

func TestAdapterCatalogContract(t *testing.T) {
	catalog := AdapterCapabilities()
	if catalog.Version != AdapterCatalogVersion || len(catalog.Adapters) != 31 || len(catalog.SHA256) != 64 {
		t.Fatalf("catalog version/count/hash = %q/%d/%q", catalog.Version, len(catalog.Adapters), catalog.SHA256)
	}
	seen := map[string]bool{}
	for index, item := range catalog.Adapters {
		if seen[item.ID] {
			t.Fatalf("duplicate adapter id %q", item.ID)
		}
		seen[item.ID] = true
		if index > 0 && catalog.Adapters[index-1].ID >= item.ID {
			t.Fatalf("adapter catalog is not stably sorted at %q", item.ID)
		}
		if item.RuntimeSupported && (len(item.Operations) == 0 || len(item.SafetyGates) == 0 || item.UnavailableReason != "") {
			t.Fatalf("callable adapter has incomplete contract: %#v", item)
		}
		if !item.RuntimeSupported && (len(item.Operations) != 0 || item.UnavailableReason == "") {
			t.Fatalf("unavailable adapter has an unsafe contract: %#v", item)
		}
	}

	wantSites := []string{"AUDIENCES", "CHD", "HDS", "HDSKY", "HHAN", "MTEAM", "OB", "PTER", "TJUPT", "TTG", "U2"}
	var sites []string
	for _, item := range catalog.Adapters {
		if item.Kind == "site" {
			sites = append(sites, item.SiteCode)
		}
	}
	slices.Sort(sites)
	if !slices.Equal(sites, wantSites) {
		t.Fatalf("site capability codes = %#v, want %#v", sites, wantSites)
	}
	for _, adapter := range []string{"imgbox", "pixhost"} {
		index := slices.IndexFunc(catalog.Adapters, func(item AdapterCapability) bool { return item.ID == "image_host/"+adapter })
		if index < 0 || !catalog.Adapters[index].RuntimeSupported || len(catalog.Adapters[index].CredentialFields) != 0 ||
			!slices.Contains(catalog.Adapters[index].Operations, "upload_image") {
			t.Fatalf("keyless image-host capability is incomplete for %s: %#v", adapter, catalog.Adapters[index])
		}
	}
}

func TestUnifiedDownloaderContractsMatchRuntimeCatalog(t *testing.T) {
	catalog := AdapterCapabilities()
	for _, downloader := range DownloaderAdapterCapabilities() {
		id := "downloader/" + downloader.Adapter
		index := slices.IndexFunc(catalog.Adapters, func(item AdapterCapability) bool { return item.ID == id })
		if index < 0 {
			t.Fatalf("unified catalog is missing %s", id)
		}
		item := catalog.Adapters[index]
		if item.RuntimeSupported != downloader.RuntimeSupported || !slices.Equal(item.CredentialFields, downloader.CredentialFields) {
			t.Fatalf("unified downloader contract drift for %s", downloader.Adapter)
		}
		operations := []struct {
			name    string
			enabled bool
		}{
			{"probe", downloader.Operations.Probe}, {"add_torrent", downloader.Operations.AddTorrent},
			{"inspect", downloader.Operations.Inspect}, {"list_torrents", downloader.Operations.ListTorrents},
			{"list_files", downloader.Operations.ListFiles},
			{"set_limits", downloader.Operations.SetLimits}, {"wait_complete", downloader.Operations.WaitComplete},
			{"category", downloader.Operations.Category}, {"tags", downloader.Operations.Tags},
			{"skip_checking", downloader.Operations.SkipChecking},
		}
		for _, operation := range operations {
			if slices.Contains(item.Operations, operation.name) != operation.enabled {
				t.Fatalf("unified operation %s drift for %s", operation.name, downloader.Adapter)
			}
		}
	}
}

func TestAdapterCatalogGoldenFingerprint(t *testing.T) {
	body, err := os.ReadFile("testdata/adapter_catalog.golden.sha256")
	if err != nil {
		t.Fatal(err)
	}
	want := strings.TrimSpace(string(body))
	got := AdapterCapabilities().SHA256
	if got != want {
		t.Fatalf("adapter catalog golden fingerprint changed: got %s want %s; review the full capability contract before updating the golden file", got, want)
	}
}
