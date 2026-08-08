package server

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeDownloaderService struct{}

func (fakeDownloaderService) Probe(context.Context, string, workflow.Actor) (qbittorrent.ProbeResult, error) {
	return qbittorrent.ProbeResult{ApplicationVersion: "v5.2.0", WebAPIVersion: "2.14.1", Authentication: "api_key"}, nil
}

func (fakeDownloaderService) Inspect(context.Context, string, string, workflow.Actor) (downloaders.TorrentEvidence, error) {
	return downloaders.TorrentEvidence{}, nil
}

func (fakeDownloaderService) Add(context.Context, string, []byte, qbittorrent.AddOptions, workflow.Actor) (downloaders.AddEvidence, error) {
	return downloaders.AddEvidence{}, nil
}

func (fakeDownloaderService) SetLimits(context.Context, string, string, int64, int64, workflow.Actor) (downloaders.TorrentEvidence, error) {
	return downloaders.TorrentEvidence{}, nil
}

func TestDownloaderProbeRouteAndInvalidTorrent(t *testing.T) {
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), Downloaders: fakeDownloaderService{},
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
		Auth: fakeAuthenticator{principal: security.Principal{
			UserID: "2cbfe1ba-d85c-4ab8-b529-50cdacb87a03", Role: "admin", TokenScopes: []string{"*"},
		}},
	})
	request := httptest.NewRequest(http.MethodPost, "/api/v2/downloaders/qbit/probe", nil)
	request.Header.Set("Authorization", "Bearer ua_test-token-value-that-is-long-enough")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !bytes.Contains(response.Body.Bytes(), []byte(`"webapi_version":"2.14.1"`)) {
		t.Fatalf("probe response = %d %s", response.Code, response.Body.String())
	}

	request = httptest.NewRequest(http.MethodPost, "/api/v2/downloaders/qbit/torrents", bytes.NewBufferString(`{"torrent_base64":"not-base64"}`))
	request.Header.Set("Authorization", "Bearer ua_test-token-value-that-is-long-enough")
	request.Header.Set("Content-Type", "application/json")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest || !bytes.Contains(response.Body.Bytes(), []byte(`"code":"invalid_torrent"`)) {
		t.Fatalf("invalid torrent response = %d %s", response.Code, response.Body.String())
	}
}
