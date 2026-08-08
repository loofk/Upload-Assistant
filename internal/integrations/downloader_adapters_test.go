package integrations

import (
	"errors"
	"testing"
)

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
	rtorrent, ok := DownloaderAdapterCapabilityFor("rtorrent")
	if !ok || !rtorrent.RuntimeSupported || !rtorrent.Operations.SetLimits || rtorrent.Operations.SkipChecking || len(rtorrent.Constraints) < 3 {
		t.Fatalf("rTorrent capability = %#v", rtorrent)
	}
	deluge, exists := DownloaderAdapterCapabilityFor("deluge")
	if !exists || !deluge.RuntimeSupported || !deluge.Operations.AddTorrent || deluge.Operations.Category || deluge.Operations.Tags || deluge.Operations.SkipChecking || len(deluge.Constraints) < 4 {
		t.Fatalf("Deluge capability = %#v", deluge)
	}
	items[0].CredentialFields[0] = "mutated"
	items[3].Constraints[0] = "mutated"
	fresh := DownloaderAdapterCapabilities()
	if fresh[0].CredentialFields[0] == "mutated" {
		t.Fatal("capability catalog returned shared credential field storage")
	}
	if fresh[3].Constraints[0] == "mutated" {
		t.Fatal("capability catalog returned shared constraint storage")
	}
}

func TestDownloaderCredentialContractRejectsUnsupportedAndPartialFields(t *testing.T) {
	rtorrent, _ := DownloaderAdapterCapabilityFor("rtorrent")
	if err := validateDownloaderCredentialContract(rtorrent, map[string]string{"api_key": "secret"}); !errors.Is(err, ErrValidation) {
		t.Fatalf("unsupported field error = %v", err)
	}
	if err := validateDownloaderCredentialContract(rtorrent, map[string]string{"username": "operator"}); !errors.Is(err, ErrValidation) {
		t.Fatalf("partial basic authentication error = %v", err)
	}
	if err := validateDownloaderCredentialContract(rtorrent, map[string]string{"username": "operator", "password": "secret"}); err != nil {
		t.Fatalf("valid basic authentication error = %v", err)
	}
	qbit, _ := DownloaderAdapterCapabilityFor("qbittorrent")
	if err := validateDownloaderCredentialContract(qbit, map[string]string{"api_key": "token"}); err != nil {
		t.Fatalf("valid qBittorrent bearer credential error = %v", err)
	}
	deluge, _ := DownloaderAdapterCapabilityFor("deluge")
	if err := validateDownloaderCredentialContract(deluge, map[string]string{"password": "secret"}); err != nil {
		t.Fatalf("valid Deluge Web credential error = %v", err)
	}
	if err := validateDownloaderCredentialContract(deluge, map[string]string{"username": "operator", "password": "secret"}); !errors.Is(err, ErrValidation) {
		t.Fatalf("Deluge native RPC credentials error = %v", err)
	}
}

func TestDownloaderOptionContractRejectsUnsupportedDelugeLabelDefaults(t *testing.T) {
	deluge, _ := DownloaderAdapterCapabilityFor("deluge")
	for _, options := range []map[string]any{{"category": "source"}, {"tag": "retorrent"}, {"label": "MTEAM"}} {
		if err := validateDownloaderOptionContract(deluge, options); !errors.Is(err, ErrValidation) {
			t.Fatalf("Deluge options %#v error = %v", options, err)
		}
	}
	if err := validateDownloaderOptionContract(deluge, map[string]any{}); err != nil {
		t.Fatalf("empty Deluge options error = %v", err)
	}
}
