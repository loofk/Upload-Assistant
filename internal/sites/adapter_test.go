package sites

import (
	"context"
	"errors"
	"testing"
)

type fakeSourceAdapter struct{ code string }

func (adapter fakeSourceAdapter) SiteCode() string { return adapter.code }
func (fakeSourceAdapter) Inspect(context.Context, SourceReference) (SourceInfo, error) {
	return SourceInfo{Name: "fixture"}, nil
}

type fakeCandidateSourceAdapter struct{ fakeSourceAdapter }

func (adapter fakeCandidateSourceAdapter) ListCandidates(context.Context, CandidateScanRequest) (CandidateScanEvidence, error) {
	return CandidateScanEvidence{SiteCode: adapter.code, Items: []SourceCandidate{{TorrentID: "7"}}}, nil
}
func (fakeSourceAdapter) Download(context.Context, SourceReference) (DownloadedTorrent, error) {
	return DownloadedTorrent{Filename: "fixture.torrent"}, nil
}

func TestRegistryRoutesCandidateListingsOnlyToCapableAdapters(t *testing.T) {
	registry, err := NewRegistry(fakeCandidateSourceAdapter{fakeSourceAdapter{code: "U2"}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := registry.ListCandidates(context.Background(), "u2", CandidateScanRequest{Limit: 10})
	if err != nil || len(result.Items) != 1 || result.Items[0].TorrentID != "7" {
		t.Fatalf("ListCandidates() = %#v/%v", result, err)
	}
	_, err = registry.ListCandidates(context.Background(), "CHD", CandidateScanRequest{})
	var adapterError *AdapterError
	if !errors.As(err, &adapterError) || adapterError.Code != "source_candidate_adapter_unavailable" {
		t.Fatalf("unavailable candidate adapter error = %#v", err)
	}
}

func TestRegistryRoutesAndRejectsUnavailableAdapter(t *testing.T) {
	registry, err := NewRegistry(fakeSourceAdapter{code: "u2"})
	if err != nil {
		t.Fatal(err)
	}
	result, err := registry.Inspect(context.Background(), SourceReference{Tracker: "U2", TorrentID: "1"})
	if err != nil || result.Name != "fixture" {
		t.Fatalf("Inspect() result/error = %#v/%v", result, err)
	}
	_, err = registry.Download(context.Background(), SourceReference{Tracker: "CHD", TorrentID: "1"})
	var adapterError *AdapterError
	if !errors.As(err, &adapterError) || adapterError.Code != "site_adapter_unavailable" {
		t.Fatalf("Download() error = %#v", err)
	}
}
