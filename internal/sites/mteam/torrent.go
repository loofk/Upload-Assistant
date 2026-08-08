package mteam

import (
	"context"

	"github.com/loofk/upload-assistant/v2/internal/sites"
)

const uploadAnnounceURL = "https://fake.tracker"

type TorrentAdapter struct{}

func NewTorrentAdapter() TorrentAdapter { return TorrentAdapter{} }

func (TorrentAdapter) SiteCode() string { return "MTEAM" }

func (TorrentAdapter) TorrentProfile(context.Context) (sites.TargetTorrentProfile, error) {
	return sites.TargetTorrentProfile{
		SiteCode: "MTEAM", Adapter: "mteam_api", AnnounceURL: uploadAnnounceURL,
		SourceTag: "MTEAM", RequiredTopLevelKeys: []string{"announce", "info"},
	}, nil
}

var _ sites.TargetTorrentAdapter = TorrentAdapter{}
