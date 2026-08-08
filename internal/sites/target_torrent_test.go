package sites

import (
	"context"
	"testing"
)

type fakeTargetTorrentAdapter struct {
	code    string
	profile TargetTorrentProfile
}

func (adapter fakeTargetTorrentAdapter) SiteCode() string { return adapter.code }
func (adapter fakeTargetTorrentAdapter) TorrentProfile(context.Context) (TargetTorrentProfile, error) {
	return adapter.profile, nil
}

func TestTargetTorrentRegistryRoutesAndValidatesProfiles(t *testing.T) {
	registry, err := NewTargetTorrentRegistry(fakeTargetTorrentAdapter{code: "MTEAM", profile: TargetTorrentProfile{
		SiteCode: "MTEAM", Adapter: "mteam_api", AnnounceURL: "https://fake.tracker",
		SourceTag: "MTEAM", RequiredTopLevelKeys: []string{"announce", "info"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	profile, err := registry.TorrentProfile(context.Background(), "mteam")
	if err != nil || profile.SourceTag != "MTEAM" {
		t.Fatalf("TorrentProfile() profile/error = %#v/%v", profile, err)
	}
	_, err = registry.TorrentProfile(context.Background(), "TTG")
	code, _, _ := ErrorDetails(err)
	if code != "target_torrent_adapter_unavailable" {
		t.Fatalf("missing adapter code/error = %q/%v", code, err)
	}
}
