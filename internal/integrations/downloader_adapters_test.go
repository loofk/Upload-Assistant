package integrations

import "testing"

func TestDownloaderAdapterCapabilitiesAreTruthfulAndCopied(t *testing.T) {
	items := DownloaderAdapterCapabilities()
	if len(items) != 4 {
		t.Fatalf("adapter count = %d, want 4", len(items))
	}
	qbit, ok := DownloaderAdapterCapabilityFor(" QBittorrent ")
	if !ok || !qbit.RuntimeSupported || !qbit.Operations.WaitComplete || !qbit.Operations.SetLimits {
		t.Fatalf("qBittorrent capability = %#v", qbit)
	}
	transmission, ok := DownloaderAdapterCapabilityFor("transmission")
	if !ok || !transmission.RuntimeSupported || !transmission.Operations.AddTorrent || !transmission.Operations.ListFiles {
		t.Fatalf("Transmission capability = %#v", transmission)
	}
	for _, adapter := range []string{"deluge", "rtorrent"} {
		item, exists := DownloaderAdapterCapabilityFor(adapter)
		if !exists || item.RuntimeSupported || item.UnavailableReason == "" {
			t.Fatalf("unsupported adapter %s capability = %#v", adapter, item)
		}
	}
	items[0].CredentialFields[0] = "mutated"
	fresh := DownloaderAdapterCapabilities()
	if fresh[0].CredentialFields[0] == "mutated" {
		t.Fatal("capability catalog returned shared credential field storage")
	}
}
