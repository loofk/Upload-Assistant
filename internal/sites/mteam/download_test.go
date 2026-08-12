package mteam

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestClientDownloadsUploadedMTeamTorrentWithoutLeakingSignedCredentials(t *testing.T) {
	torrent := mteamUploadTorrent([]byte("abc"))
	request := mteamTargetTorrentDownloadRequest(t, torrent)
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/torrent/genDlToken":
			if r.Method != http.MethodPost || r.Header.Get("x-api-key") != "mteam-secret" {
				t.Fatalf("token method/key = %s/%q", r.Method, r.Header.Get("x-api-key"))
			}
			if err := r.ParseForm(); err != nil || r.Form.Get("id") != "98765" {
				t.Fatalf("token form/error = %#v/%v", r.Form, err)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"code": 0, "data": server.URL + "/signed-download?token=signed-secret",
			})
		case "/signed-download":
			if r.Method != http.MethodGet || r.URL.Query().Get("token") != "signed-secret" || r.Header.Get("x-api-key") != "" {
				t.Fatalf("download method/query/key = %s/%s/%q", r.Method, r.URL.RawQuery, r.Header.Get("x-api-key"))
			}
			w.Header().Set("Content-Type", "application/x-bittorrent")
			_, _ = w.Write(torrent)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	store := runtimeSiteStore(server.URL, "mteam-secret")
	result, err := NewClient(store, permissiveAccessGate{}, nil).DownloadUploadedTorrent(context.Background(), request, workflow.Actor{Type: "worker", ID: "fixture"})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(result.Evidence.Filename, "98765.torrent") || result.Evidence.SHA256 == "" ||
		result.Evidence.ContentFingerprint != request.ContentFingerprintSHA256 || len(store.actions) != 2 ||
		store.actions[0] != "target.torrent_download_intent" || store.actions[1] != "target.torrent_download_result" {
		t.Fatalf("download evidence/actions = %#v/%#v", result.Evidence, store.actions)
	}
	encoded, _ := json.Marshal(map[string]any{"result": result, "audit": store.details})
	for _, secret := range []string{"mteam-secret", "signed-secret", "fake.tracker"} {
		if strings.Contains(string(encoded), secret) {
			t.Fatalf("download evidence/audit exposed %q", secret)
		}
	}
}

func TestClientRejectsUntrustedDownloadURLBeforeGET(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"code":0,"data":"https://example.com/torrent?token=secret"}`))
	}))
	defer server.Close()
	store := runtimeSiteStore(server.URL, "key")
	_, err := NewClient(store, permissiveAccessGate{}, nil).DownloadUploadedTorrent(
		context.Background(), mteamTargetTorrentDownloadRequest(t, mteamUploadTorrent([]byte("abc"))), workflow.Actor{},
	)
	code, _, _ := sites.ErrorDetails(err)
	if code != "target_torrent_download_url_rejected" || len(store.actions) != 2 || store.actions[1] != "target.torrent_download_outcome" {
		t.Fatalf("untrusted URL code/actions/error = %q/%#v/%v", code, store.actions, err)
	}
}

func TestClientRejectsDownloadedTorrentPayloadMismatch(t *testing.T) {
	expected := mteamUploadTorrent([]byte("abc"))
	actual := mteamUploadTorrent([]byte("abcd"))
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/torrent/genDlToken" {
			_ = json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": server.URL + "/download"})
			return
		}
		_, _ = w.Write(actual)
	}))
	defer server.Close()
	store := runtimeSiteStore(server.URL, "key")
	_, err := NewClient(store, permissiveAccessGate{}, nil).DownloadUploadedTorrent(
		context.Background(), mteamTargetTorrentDownloadRequest(t, expected), workflow.Actor{},
	)
	code, _, _ := sites.ErrorDetails(err)
	if code != "target_torrent_payload_mismatch" || len(store.actions) != 2 || store.actions[1] != "target.torrent_download_outcome" {
		t.Fatalf("payload mismatch code/actions/error = %q/%#v/%v", code, store.actions, err)
	}
}

func mteamTargetTorrentDownloadRequest(t *testing.T, torrent []byte) sites.TargetTorrentDownloadRequest {
	t.Helper()
	inspection, err := torrentmeta.Inspect(torrent)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(torrent)
	return sites.TargetTorrentDownloadRequest{
		JobID: "job-id", AttemptID: "attempt-id", TorrentID: "98765",
		UploadReceiptSHA256: strings.Repeat("1", 64), SubmittedTorrentSHA256: hex.EncodeToString(digest[:]),
		ContentFingerprintSHA256: inspection.ContentFingerprint,
	}
}

func TestValidateSignedDownloadURLAllowsConfiguredHostAndRejectsPlainHTTP(t *testing.T) {
	endpoint, _ := url.Parse("https://api.m-team.cc")
	custom, _ := url.Parse("https://downloads.example.net/file?token=secret")
	if err := validateSignedDownloadURL(custom, endpoint, []string{"downloads.example.net"}); err != nil {
		t.Fatal(err)
	}
	plain, _ := url.Parse("http://downloads.example.net/file")
	if err := validateSignedDownloadURL(plain, endpoint, []string{"downloads.example.net"}); err == nil {
		t.Fatal("expected plain HTTP download URL to be rejected")
	}
}
