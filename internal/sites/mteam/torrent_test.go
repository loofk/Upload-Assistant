package mteam

import (
	"context"
	"testing"
)

func TestTorrentProfileUsesSanitizedMTeamUploadMetadata(t *testing.T) {
	profile, err := NewTorrentAdapter().TorrentProfile(context.Background())
	if err != nil || profile.SiteCode != "MTEAM" || profile.AnnounceURL != "https://fake.tracker" ||
		profile.SourceTag != "MTEAM" || len(profile.RequiredTopLevelKeys) != 2 {
		t.Fatalf("TorrentProfile() profile/error = %#v/%v", profile, err)
	}
}
