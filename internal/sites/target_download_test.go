package sites

import (
	"context"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeTargetTorrentDownloadAdapter struct{ code string }

func (adapter fakeTargetTorrentDownloadAdapter) SiteCode() string { return adapter.code }
func (fakeTargetTorrentDownloadAdapter) DownloadUploadedTorrent(context.Context, TargetTorrentDownloadRequest, workflow.Actor) (DownloadedTargetTorrent, error) {
	return DownloadedTargetTorrent{Evidence: TargetTorrentDownloadEvidence{TorrentID: "1"}}, nil
}

func TestTargetTorrentDownloadRegistryRoutesByNormalizedSiteCode(t *testing.T) {
	registry, err := NewTargetTorrentDownloadRegistry(fakeTargetTorrentDownloadAdapter{code: "MTEAM"})
	if err != nil {
		t.Fatal(err)
	}
	result, err := registry.DownloadUploadedTorrent(context.Background(), "mteam", TargetTorrentDownloadRequest{}, workflow.Actor{})
	if err != nil || result.Evidence.TorrentID != "1" {
		t.Fatalf("download result/error = %#v/%v", result, err)
	}
	_, err = registry.DownloadUploadedTorrent(context.Background(), "U2", TargetTorrentDownloadRequest{}, workflow.Actor{})
	code, _, _ := ErrorDetails(err)
	if code != "target_torrent_download_adapter_unavailable" {
		t.Fatalf("unsupported adapter code = %q", code)
	}
}
