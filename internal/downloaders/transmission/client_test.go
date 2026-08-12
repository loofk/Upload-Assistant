package transmission

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

func TestClientSessionProbeAddInspectFilesLimitsAndWait(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	const sessionID = "transmission-session-fixture"
	var mu sync.Mutex
	methods := []string{}
	var addArguments, setArguments map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		username, password, authenticated := r.BasicAuth()
		if !authenticated || username != "operator" || password != "secret" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if r.Header.Get("X-Transmission-Session-Id") != sessionID {
			w.Header().Set("X-Transmission-Session-Id", sessionID)
			http.Error(w, "session required", http.StatusConflict)
			return
		}
		var request struct {
			Method    string         `json:"method"`
			Arguments map[string]any `json:"arguments"`
			Tag       int64          `json:"tag"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		mu.Lock()
		methods = append(methods, request.Method)
		mu.Unlock()
		arguments := any(map[string]any{})
		switch request.Method {
		case "session-get":
			arguments = map[string]any{"version": "4.0.6", "rpc-version": 17, "rpc-version-minimum": 14}
		case "torrent-add":
			mu.Lock()
			addArguments = request.Arguments
			mu.Unlock()
			arguments = map[string]any{"torrent-added": map[string]any{"id": 7, "name": "test", "hashString": hashes.V1SHA1}}
		case "torrent-set":
			mu.Lock()
			setArguments = request.Arguments
			mu.Unlock()
		case "torrent-get":
			fields := stringSlice(request.Arguments["fields"])
			if contains(fields, "files") {
				arguments = map[string]any{"torrents": []any{map[string]any{
					"hashString": hashes.V1SHA1,
					"files":      []any{map[string]any{"name": "test", "length": 1, "bytesCompleted": 1}},
					"fileStats":  []any{map[string]any{"wanted": true, "priority": 1}},
				}}}
			} else {
				arguments = map[string]any{"torrents": []any{map[string]any{
					"hashString": hashes.V1SHA1, "name": "test", "status": 6, "percentDone": 1.0,
					"totalSize": 1, "leftUntilDone": 0, "downloadedEver": 1, "uploadedEver": 3,
					"downloadLimit": 4, "downloadLimited": true, "uploadLimit": 8, "uploadLimited": true,
					"uploadRatio": 3.0, "downloadDir": "/downloads", "labels": []string{"U2", "retorrent"},
					"addedDate": 100, "doneDate": 101, "secondsDownloading": 2, "secondsSeeding": 5,
				}}}
			}
		default:
			http.Error(w, "unexpected method", http.StatusBadRequest)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"result": "success", "arguments": arguments, "tag": request.Tag})
	}))
	t.Cleanup(server.Close)

	client, err := New(Config{
		Endpoint: server.URL, Credentials: map[string]string{"username": "operator", "password": "secret"},
		HTTPClient: &http.Client{Timeout: 5 * time.Second},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	probe, err := client.Probe(ctx)
	if err != nil || probe.ApplicationVersion != "4.0.6" || probe.WebAPIVersion != "rpc-17 (min 14)" || probe.Authentication != "basic" {
		t.Fatalf("Probe() result/error = %#v/%v", probe, err)
	}
	result, err := client.Add(ctx, metainfo, qbittorrent.AddOptions{
		SavePath: "/downloads", Category: "U2", Tags: []string{"retorrent", "U2"},
		DownloadLimit: 4096, UploadLimit: 8192,
	})
	if err != nil || result.Observed == nil || result.Observed.Hash != hashes.V1SHA1 || result.Observed.ContentPath != "/downloads/test" {
		t.Fatalf("Add() result/error = %#v/%v", result, err)
	}
	torrents, err := client.List(ctx)
	if err != nil || len(torrents) != 1 || torrents[0].Ratio != 3 {
		t.Fatalf("List() torrents/error = %#v/%v", torrents, err)
	}
	files, err := client.Files(ctx, hashes.V1SHA1)
	if err != nil || len(files) != 1 || files[0].Name != "test" || !files[0].Seed {
		t.Fatalf("Files() result/error = %#v/%v", files, err)
	}
	if err := client.SetLimits(ctx, hashes.V1SHA1, 4096, 8192); err != nil {
		t.Fatal(err)
	}
	completed, err := client.WaitComplete(ctx, hashes.V1SHA1, time.Millisecond)
	if err != nil || completed.Progress != 1 || completed.State != "seeding" {
		t.Fatalf("WaitComplete() result/error = %#v/%v", completed, err)
	}

	mu.Lock()
	defer mu.Unlock()
	if addArguments["download-dir"] != "/downloads" || addArguments["download-limit"] != nil || addArguments["upload-limit"] != nil {
		t.Fatalf("torrent-add arguments = %#v", addArguments)
	}
	labels := stringSlice(addArguments["labels"])
	if strings.Join(labels, ",") != "U2,retorrent" {
		t.Fatalf("torrent-add labels = %#v", labels)
	}
	if setArguments["download-limit"] != float64(4) || setArguments["upload-limit"] != float64(8) || setArguments["download-limited"] != true || setArguments["upload-limited"] != true {
		t.Fatalf("torrent-set arguments = %#v", setArguments)
	}
	if len(methods) < 6 || methods[0] != "session-get" {
		t.Fatalf("RPC methods = %#v", methods)
	}
}

func TestClientNegotiatesTransmission41JSONRPC(t *testing.T) {
	const sessionID = "transmission-41-session"
	hash := strings.Repeat("a", 40)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Transmission-Session-Id") != sessionID {
			w.Header().Set("X-Transmission-Session-Id", sessionID)
			w.Header().Set("X-Transmission-Rpc-Version", "6.0.0")
			http.Error(w, "session required", http.StatusConflict)
			return
		}
		var request struct {
			JSONRPC string         `json:"jsonrpc"`
			Method  string         `json:"method"`
			Params  map[string]any `json:"params"`
			ID      int64          `json:"id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if request.JSONRPC != "2.0" || strings.Contains(request.Method, "-") {
			http.Error(w, "expected JSON-RPC 2.0 snake_case", http.StatusBadRequest)
			return
		}
		var result any
		switch request.Method {
		case "session_get":
			result = map[string]any{"version": "4.1.0", "rpc_version": 18, "rpc_version_minimum": 18}
		case "torrent_get":
			fields := stringSlice(request.Params["fields"])
			if !contains(fields, "hash_string") || !contains(fields, "download_dir") {
				http.Error(w, "fields were not converted to snake_case", http.StatusBadRequest)
				return
			}
			result = map[string]any{"torrents": []any{map[string]any{
				"hash_string": hash, "name": "release", "status": 6, "percent_done": 1.0,
				"total_size": 10, "left_until_done": 0, "download_dir": "/downloads", "labels": []string{"MTEAM"},
			}}}
		default:
			http.Error(w, "unexpected method", http.StatusBadRequest)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "result": result, "id": request.ID})
	}))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, HTTPClient: &http.Client{Timeout: 5 * time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	probe, err := client.Probe(context.Background())
	if err != nil || probe.ApplicationVersion != "4.1.0" || probe.WebAPIVersion != "rpc-18 (min 18)" {
		t.Fatalf("Probe() result/error = %#v/%v", probe, err)
	}
	item, err := client.Get(context.Background(), hash)
	if err != nil || item.Hash != hash || item.ContentPath != "/downloads/release" || item.State != "seeding" {
		t.Fatalf("Get() result/error = %#v/%v", item, err)
	}
}

func TestClientReturnsAuditablePartialAddWhenLimitApplicationFails(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	const sessionID = "partial-add-session"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Transmission-Session-Id") != sessionID {
			w.Header().Set("X-Transmission-Session-Id", sessionID)
			http.Error(w, "session required", http.StatusConflict)
			return
		}
		var request struct {
			Method string `json:"method"`
			Tag    int64  `json:"tag"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		result := "success"
		arguments := map[string]any{}
		if request.Method == "torrent-add" {
			arguments["torrent-added"] = map[string]any{"hashString": hashes.V1SHA1, "name": "test", "id": 1}
		} else if request.Method == "torrent-set" {
			result = "permission denied"
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"result": result, "arguments": arguments, "tag": request.Tag})
	}))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, HTTPClient: &http.Client{Timeout: 5 * time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.Add(context.Background(), metainfo, qbittorrent.AddOptions{UploadLimit: 4096})
	var partial *PartialAddError
	if !errors.As(err, &partial) || partial.Hash != hashes.V1SHA1 || result.Hashes.V1SHA1 != hashes.V1SHA1 {
		t.Fatalf("Add() result/error = %#v/%#v", result, err)
	}
}

func TestClientRejectsUnsafeEndpointCredentialsAndInvalidInputs(t *testing.T) {
	if _, err := New(Config{Endpoint: "http://user:secret@localhost:9091/transmission/rpc"}); err == nil {
		t.Fatal("New() accepted endpoint credentials")
	}
	if _, err := New(Config{Endpoint: "http://localhost:9091", Credentials: map[string]string{"username": "only"}}); err == nil {
		t.Fatal("New() accepted partial basic authentication")
	}
	client, err := New(Config{Endpoint: "http://localhost:9091"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Get(context.Background(), "not-a-hash"); err == nil || !strings.Contains(err.Error(), "invalid") {
		t.Fatalf("Get() invalid hash error = %v", err)
	}
	if err := client.SetLimits(context.Background(), strings.Repeat("a", 40), -1, 0); err == nil {
		t.Fatal("SetLimits() accepted negative limit")
	}
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	if _, err := client.Add(context.Background(), metainfo, qbittorrent.AddOptions{SkipChecking: true}); err == nil || !strings.Contains(err.Error(), "skip_checking") {
		t.Fatalf("Add() skip_checking error = %v", err)
	}
}

func stringSlice(value any) []string {
	items, _ := value.([]any)
	result := make([]string, 0, len(items))
	for _, item := range items {
		if text, ok := item.(string); ok {
			result = append(result, text)
		}
	}
	return result
}

func contains(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}
