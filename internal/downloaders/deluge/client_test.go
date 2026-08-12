package deluge

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

type delugeFixture struct {
	testing         *testing.T
	password        string
	hash            string
	connected       bool
	methods         []string
	mu              sync.Mutex
	calls           []string
	addFilename     string
	addMetainfo     []byte
	addOptions      map[string]any
	setOptions      map[string]any
	downloadKiB     float64
	uploadKiB       float64
	statusFailure   bool
	misreportLimits bool
}

func (fixture *delugeFixture) handler(w http.ResponseWriter, request *http.Request) {
	if request.URL.Path != "/json" || request.Method != http.MethodPost {
		http.NotFound(w, request)
		return
	}
	var call rpcRequest
	if err := json.NewDecoder(request.Body).Decode(&call); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	fixture.mu.Lock()
	fixture.calls = append(fixture.calls, call.Method)
	fixture.mu.Unlock()
	if call.Method != "auth.login" {
		cookie, err := request.Cookie("_session_id")
		if err != nil || cookie.Value != "fixture-session" {
			fixture.write(w, call.ID, nil, map[string]any{"code": 1, "message": "Not authenticated"})
			return
		}
	}
	var result any
	switch call.Method {
	case "auth.login":
		if len(call.Params) != 1 || stringValue(call.Params[0]) != fixture.password {
			result = false
		} else {
			http.SetCookie(w, &http.Cookie{Name: "_session_id", Value: "fixture-session", Path: "/", HttpOnly: true})
			result = true
		}
	case "web.connected":
		result = fixture.connected
	case "daemon.get_method_list":
		result = fixture.methods
	case "daemon.get_version":
		result = "2.2.0"
	case "core.get_libtorrent_version":
		result = "2.0.11.0"
	case "core.add_torrent_file":
		if len(call.Params) != 3 {
			fixture.write(w, call.ID, nil, map[string]any{"code": 2, "message": "invalid add arguments"})
			return
		}
		encoded, ok := call.Params[1].(string)
		if !ok {
			fixture.write(w, call.ID, nil, map[string]any{"code": 2, "message": "invalid metainfo"})
			return
		}
		metainfo, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil {
			fixture.write(w, call.ID, nil, map[string]any{"code": 2, "message": "invalid base64"})
			return
		}
		options, _ := call.Params[2].(map[string]any)
		fixture.mu.Lock()
		fixture.addFilename, fixture.addMetainfo, fixture.addOptions = stringValue(call.Params[0]), metainfo, options
		fixture.downloadKiB, _ = options["max_download_speed"].(float64)
		fixture.uploadKiB, _ = options["max_upload_speed"].(float64)
		fixture.mu.Unlock()
		result = strings.ToUpper(fixture.hash)
	case "core.set_torrent_options":
		if len(call.Params) != 2 {
			fixture.write(w, call.ID, nil, map[string]any{"code": 2, "message": "invalid set arguments"})
			return
		}
		options, _ := call.Params[1].(map[string]any)
		fixture.mu.Lock()
		fixture.setOptions = options
		fixture.downloadKiB, _ = options["max_download_speed"].(float64)
		fixture.uploadKiB, _ = options["max_upload_speed"].(float64)
		fixture.mu.Unlock()
		result = nil
	case "core.get_torrent_status", "core.get_torrents_status":
		fixture.mu.Lock()
		failed := fixture.statusFailure
		downloadKiB, uploadKiB := fixture.downloadKiB, fixture.uploadKiB
		if fixture.misreportLimits {
			downloadKiB, uploadKiB = -1, -1
		}
		fixture.mu.Unlock()
		if failed {
			fixture.write(w, call.ID, nil, map[string]any{"code": 3, "message": "unknown torrent"})
			return
		}
		status := map[string]any{
			"hash": strings.ToUpper(fixture.hash), "name": "release", "state": "Seeding", "progress": 100,
			"total_size": 1024, "total_done": 1024, "total_remaining": 0,
			"total_payload_download": 1024, "total_uploaded": 2048,
			"download_payload_rate": 0, "upload_payload_rate": 128,
			"max_download_speed": downloadKiB, "max_upload_speed": uploadKiB, "ratio": 2.0,
			"save_path": "/downloads", "time_added": 100, "completed_time": 110,
			"active_time": 20, "seeding_time": 10, "message": "", "is_seed": true,
			"files":         []any{map[string]any{"index": 0, "path": "release/video.mkv", "size": 1024, "offset": 0}},
			"file_progress": []float64{1}, "file_priorities": []int{4},
		}
		result = status
		if call.Method == "core.get_torrents_status" {
			result = map[string]any{fixture.hash: status}
		}
	default:
		fixture.write(w, call.ID, nil, map[string]any{"code": 4, "message": "unknown method"})
		return
	}
	fixture.write(w, call.ID, result, nil)
}

func (fixture *delugeFixture) write(w http.ResponseWriter, id int64, result, rpcFailure any) {
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(map[string]any{"result": result, "error": rpcFailure, "id": id}); err != nil {
		fixture.testing.Errorf("encode fixture response: %v", err)
	}
}

func TestClientProbeAddInspectFilesLimitsAndWait(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1024e4:name7:releaseee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &delugeFixture{
		testing: t, password: "secret", hash: hashes.V1SHA1, connected: true,
		methods: append([]string(nil), requiredMethods...), downloadKiB: -1, uploadKiB: -1,
	}
	server := httptest.NewServer(http.HandlerFunc(fixture.handler))
	t.Cleanup(server.Close)
	client, err := New(Config{
		Endpoint: server.URL, Credentials: map[string]string{"password": "secret"},
		HTTPClient: &http.Client{Timeout: 5 * time.Second},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	probe, err := client.Probe(ctx)
	if err != nil || probe.ApplicationVersion != "Deluge 2.2.0" || probe.WebAPIVersion != "JSON-RPC v1 (libtorrent 2.0.11.0)" || probe.Authentication != "deluge-web-cookie" {
		t.Fatalf("Probe() result/error = %#v/%v", probe, err)
	}
	result, err := client.Add(ctx, metainfo, qbittorrent.AddOptions{
		SavePath: "/downloads", Paused: true, DownloadLimit: 4096, UploadLimit: 8192,
	})
	if err != nil || result.Observed == nil || result.Observed.Hash != hashes.V1SHA1 || result.Observed.ContentPath != "/downloads/release" {
		t.Fatalf("Add() result/error = %#v/%v", result, err)
	}
	if result.Observed.DownloadLimit != 4096 || result.Observed.UploadLimit != 8192 || result.Observed.State != "seeding" {
		t.Fatalf("Add() observed = %#v", result.Observed)
	}
	torrents, err := client.List(ctx)
	if err != nil || len(torrents) != 1 || torrents[0].Name != "release" {
		t.Fatalf("List() torrents/error = %#v/%v", torrents, err)
	}
	files, err := client.Files(ctx, hashes.V1SHA1)
	if err != nil || len(files) != 1 || files[0].Name != "release/video.mkv" || files[0].Priority != 4 || !files[0].Seed {
		t.Fatalf("Files() result/error = %#v/%v", files, err)
	}
	if err := client.SetLimits(ctx, hashes.V1SHA1, 2048, 3072); err != nil {
		t.Fatal(err)
	}
	completed, err := client.WaitComplete(ctx, hashes.V1SHA1, time.Millisecond)
	if err != nil || completed.Progress != 1 || completed.SeedingTime != 10 {
		t.Fatalf("WaitComplete() result/error = %#v/%v", completed, err)
	}

	fixture.mu.Lock()
	defer fixture.mu.Unlock()
	if fixture.addFilename != hashes.V1SHA1+".torrent" || string(fixture.addMetainfo) != string(metainfo) {
		t.Fatalf("add payload filename=%q metainfo=%q", fixture.addFilename, fixture.addMetainfo)
	}
	if fixture.addOptions["download_location"] != "/downloads" || fixture.addOptions["add_paused"] != true || fixture.addOptions["seed_mode"] != false || fixture.addOptions["max_download_speed"] != float64(4) || fixture.addOptions["max_upload_speed"] != float64(8) {
		t.Fatalf("add options = %#v", fixture.addOptions)
	}
	if fixture.setOptions["max_download_speed"] != float64(2) || fixture.setOptions["max_upload_speed"] != float64(3) {
		t.Fatalf("set options = %#v", fixture.setOptions)
	}
	if count(fixture.calls, "auth.login") != 1 || !slices.Contains(fixture.calls, "core.add_torrent_file") {
		t.Fatalf("RPC calls = %#v", fixture.calls)
	}
}

func TestClientRequiresWebAuthenticationAndConnectedDaemon(t *testing.T) {
	fixture := &delugeFixture{testing: t, password: "secret", connected: false}
	server := httptest.NewServer(http.HandlerFunc(fixture.handler))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL + "/", Credentials: map[string]string{"password": "wrong"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Probe(context.Background()); !errors.Is(err, qbittorrent.ErrUnauthorized) {
		t.Fatalf("Probe() bad password error = %v", err)
	}
	client, err = New(Config{Endpoint: server.URL, Credentials: map[string]string{"password": "secret"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Probe(context.Background()); err == nil || !strings.Contains(err.Error(), "not connected") {
		t.Fatalf("Probe() disconnected daemon error = %v", err)
	}
}

func TestClientRejectsMissingDaemonMethods(t *testing.T) {
	fixture := &delugeFixture{testing: t, password: "secret", connected: true, methods: []string{"daemon.get_method_list"}}
	server := httptest.NewServer(http.HandlerFunc(fixture.handler))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, Credentials: map[string]string{"password": "secret"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Probe(context.Background()); err == nil || !strings.Contains(err.Error(), "core.add_torrent_file") {
		t.Fatalf("Probe() missing method error = %v", err)
	}
}

func TestClientReturnsAuditablePartialAddAfterObservedHash(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &delugeFixture{testing: t, password: "secret", hash: hashes.V1SHA1, connected: true, statusFailure: true}
	server := httptest.NewServer(http.HandlerFunc(fixture.handler))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, Credentials: map[string]string{"password": "secret"}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.Add(context.Background(), metainfo, qbittorrent.AddOptions{})
	var partial *PartialAddError
	if !errors.As(err, &partial) || partial.Hash != hashes.V1SHA1 || result.Hashes.V1SHA1 != hashes.V1SHA1 {
		t.Fatalf("Add() result/error = %#v/%#v", result, err)
	}
}

func TestClientReturnsAuditablePartialAddWhenRateLimitReadbackFails(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &delugeFixture{testing: t, password: "secret", hash: hashes.V1SHA1, connected: true, misreportLimits: true}
	server := httptest.NewServer(http.HandlerFunc(fixture.handler))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, Credentials: map[string]string{"password": "secret"}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.Add(context.Background(), metainfo, qbittorrent.AddOptions{UploadLimit: 4096})
	var partial *PartialAddError
	if !errors.As(err, &partial) || partial.Hash != hashes.V1SHA1 || result.Hashes.V1SHA1 != hashes.V1SHA1 || !strings.Contains(err.Error(), "outside requested cap") {
		t.Fatalf("Add() limit readback result/error = %#v/%#v", result, err)
	}
}

func TestClientRejectsUnsupportedOptionsBeforeRemoteMutation(t *testing.T) {
	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls.Add(1) }))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, Credentials: map[string]string{"password": "secret"}})
	if err != nil {
		t.Fatal(err)
	}
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	for _, options := range []qbittorrent.AddOptions{{Category: "U2"}, {Tags: []string{"retorrent"}}, {SkipChecking: true}, {SavePath: "relative/path"}} {
		if _, err := client.Add(context.Background(), metainfo, options); err == nil {
			t.Fatalf("Add() accepted unsupported options %#v", options)
		}
	}
	if calls.Load() != 0 {
		t.Fatalf("Add() made %d calls before validating options", calls.Load())
	}
}

func TestClientRejectsUnsafeConfigurationAndInvalidInputs(t *testing.T) {
	if _, err := New(Config{Endpoint: "http://operator:secret@localhost:8112/json", Credentials: map[string]string{"password": "secret"}}); err == nil {
		t.Fatal("New() accepted endpoint credentials")
	}
	if _, err := New(Config{Endpoint: "http://localhost:8112/%2e%2e/json", Credentials: map[string]string{"password": "secret"}}); err == nil {
		t.Fatal("New() accepted an encoded path traversal")
	}
	if _, err := New(Config{Endpoint: "http://localhost:8112"}); !errors.Is(err, qbittorrent.ErrUnauthorized) {
		t.Fatalf("New() missing password error = %v", err)
	}
	if _, err := New(Config{Endpoint: "http://localhost:8112", Credentials: map[string]string{"username": "native", "password": "secret"}}); err == nil {
		t.Fatal("New() accepted native daemon credentials")
	}
	client, err := New(Config{Endpoint: "http://localhost:8112", Credentials: map[string]string{"password": "secret"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Get(context.Background(), "not-a-hash"); err == nil {
		t.Fatal("Get() accepted an invalid hash")
	}
	if err := client.SetLimits(context.Background(), strings.Repeat("a", 40), -1, 0); err == nil {
		t.Fatal("SetLimits() accepted a negative limit")
	}
}

func stringValue(value any) string {
	text, _ := value.(string)
	return text
}

func count(values []string, wanted string) int {
	total := 0
	for _, value := range values {
		if value == wanted {
			total++
		}
	}
	return total
}
