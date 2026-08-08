package server

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeDownloaderService struct {
	addEvidence   downloaders.AddEvidence
	addErr        error
	limitEvidence downloaders.TorrentEvidence
	limitErr      error
}

func (fakeDownloaderService) Probe(context.Context, string, workflow.Actor) (qbittorrent.ProbeResult, error) {
	return qbittorrent.ProbeResult{ApplicationVersion: "v5.2.0", WebAPIVersion: "2.14.1", Authentication: "api_key"}, nil
}

func (fakeDownloaderService) Inspect(context.Context, string, string, workflow.Actor) (downloaders.TorrentEvidence, error) {
	return downloaders.TorrentEvidence{}, nil
}

func (service fakeDownloaderService) Add(context.Context, string, []byte, qbittorrent.AddOptions, workflow.Actor) (downloaders.AddEvidence, error) {
	return service.addEvidence, service.addErr
}

func TestDownloaderAddUnknownOutcomeReturnsReconciliationEvidence(t *testing.T) {
	hash := "0123456789abcdef0123456789abcdef01234567"
	service := fakeDownloaderService{
		addEvidence: downloaders.AddEvidence{
			DownloaderName: "qbit", Adapter: "qbittorrent", TorrentSHA256: strings.Repeat("a", 64),
			Result: qbittorrent.AddResult{Hashes: torrentmeta.InfoHashes{V1SHA1: hash}},
		},
		addErr: fmt.Errorf("%w: fixture transport error", downloaders.ErrAddOutcomeUnknown),
	}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), Downloaders: service,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
		Auth: fakeAuthenticator{principal: security.Principal{
			UserID: "2cbfe1ba-d85c-4ab8-b529-50cdacb87a03", Role: "admin", TokenScopes: []string{"*"},
		}},
	})
	body := `{"torrent_base64":"` + base64.StdEncoding.EncodeToString([]byte("torrent")) + `"}`
	request := httptest.NewRequest(http.MethodPost, "/api/v2/downloaders/qbit/torrents", bytes.NewBufferString(body))
	request.Header.Set("Authorization", "Bearer ua_test-token-value-that-is-long-enough")
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusConflict || !bytes.Contains(response.Body.Bytes(), []byte(`"status":"blocked"`)) ||
		!bytes.Contains(response.Body.Bytes(), []byte(`"code":"downloader_add_outcome_unknown"`)) ||
		!bytes.Contains(response.Body.Bytes(), []byte(`"observed_hash":"`+hash+`"`)) ||
		!bytes.Contains(response.Body.Bytes(), []byte(`"evidence"`)) {
		t.Fatalf("unknown add response = %d %s", response.Code, response.Body.String())
	}
}

func (service fakeDownloaderService) SetLimits(context.Context, string, string, int64, int64, workflow.Actor) (downloaders.TorrentEvidence, error) {
	return service.limitEvidence, service.limitErr
}

func TestDownloaderLimitUnknownOutcomeReturnsReconciliationEvidence(t *testing.T) {
	hash := strings.Repeat("a", 40)
	service := fakeDownloaderService{
		limitEvidence: downloaders.TorrentEvidence{
			DownloaderName: "qbit", Adapter: "qbittorrent", ConfigurationSHA256: strings.Repeat("b", 64),
			Torrent: qbittorrent.Torrent{Hash: hash, DownloadLimit: 4096, UploadLimit: 0},
		},
		limitErr: fmt.Errorf("%w: fixture read-back failure", downloaders.ErrLimitsOutcomeUnknown),
	}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), Downloaders: service,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
		Auth: fakeAuthenticator{principal: security.Principal{
			UserID: "2cbfe1ba-d85c-4ab8-b529-50cdacb87a03", Role: "admin", TokenScopes: []string{"*"},
		}},
	})
	request := httptest.NewRequest(
		http.MethodPost, "/api/v2/downloaders/qbit/torrents/"+hash+"/limits",
		bytes.NewBufferString(`{"download_limit":4096,"upload_limit":8192}`),
	)
	request.Header.Set("Authorization", "Bearer ua_test-token-value-that-is-long-enough")
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusConflict || !bytes.Contains(response.Body.Bytes(), []byte(`"status":"blocked"`)) ||
		!bytes.Contains(response.Body.Bytes(), []byte(`"code":"downloader_limits_outcome_unknown"`)) ||
		!bytes.Contains(response.Body.Bytes(), []byte(`"torrent_hash":"`+hash+`"`)) ||
		!bytes.Contains(response.Body.Bytes(), []byte(`"upload_limit":8192`)) ||
		!bytes.Contains(response.Body.Bytes(), []byte(`"evidence"`)) {
		t.Fatalf("unknown limits response = %d %s", response.Code, response.Body.String())
	}
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
