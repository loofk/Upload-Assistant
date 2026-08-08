package downloaders

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeConfigurationStore struct {
	runtime      integrations.RuntimeDownloader
	healthStatus string
	auditActions []string
}

func (store *fakeConfigurationStore) GetRuntimeDownloader(context.Context, string) (integrations.RuntimeDownloader, error) {
	return store.runtime, nil
}

func (store *fakeConfigurationStore) RecordDownloaderHealth(_ context.Context, _ string, status string, _ map[string]any, _ workflow.Actor) error {
	store.healthStatus = status
	return nil
}

func (store *fakeConfigurationStore) AuditDownloaderAction(_ context.Context, _ string, action string, _ map[string]any, _ workflow.Actor) error {
	store.auditActions = append(store.auditActions, action)
	return nil
}

func TestManagerProbeInspectAndPathMapping(t *testing.T) {
	hash := "0123456789abcdef0123456789abcdef01234567"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer qbt_test" {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		switch r.URL.Path {
		case "/api/v2/app/version":
			_, _ = io.WriteString(w, "v5.2.0")
		case "/api/v2/app/webapiVersion":
			_, _ = io.WriteString(w, "2.14.1")
		case "/api/v2/torrents/info":
			_ = json.NewEncoder(w).Encode([]map[string]any{{
				"hash": hash, "name": "release", "state": "uploading", "progress": 1,
				"total_size": 1024, "completed": 1024, "amount_left": 0,
				"save_path": "/remote/downloads", "content_path": "/remote/downloads/release",
			}})
		case "/api/v2/torrents/files":
			_ = json.NewEncoder(w).Encode([]map[string]any{{
				"index": 0, "name": "release/video.mkv", "size": 1024, "progress": 1,
				"priority": 1, "is_seed": true, "availability": 1,
			}})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	store := &fakeConfigurationStore{runtime: integrations.RuntimeDownloader{
		Downloader: integrations.Downloader{
			Name: "qbit", Adapter: "qbittorrent", Enabled: true,
			PathMappings: []integrations.PathMapping{{RemotePath: "/remote/downloads", LocalPath: "/downloads", Priority: 100}},
		},
		EndpointConfig: integrations.EndpointConfig{Endpoint: server.URL, TimeoutSeconds: 5},
		Credentials:    map[string]string{"api_key": "qbt_test"},
	}}
	manager := NewManager(store)
	actor := workflow.Actor{Type: "test", ID: "manager"}
	probe, err := manager.Probe(context.Background(), "qbit", actor)
	if err != nil || probe.WebAPIVersion != "2.14.1" || store.healthStatus != "ready" {
		t.Fatalf("Probe() result/health/error = %#v/%s/%v", probe, store.healthStatus, err)
	}
	evidence, err := manager.Inspect(context.Background(), "qbit", hash, actor)
	if err != nil {
		t.Fatal(err)
	}
	if evidence.LocalContentPath != "/downloads/release" || evidence.PathMapping == nil {
		t.Fatalf("path evidence = %#v", evidence)
	}
	files, err := manager.Files(context.Background(), "qbit", hash, actor)
	if err != nil || files.FileCount != 1 || files.TotalSize != 1024 || files.Torrent.LocalContentPath != "/downloads/release" {
		t.Fatalf("Files() evidence/error = %#v/%v", files, err)
	}
	if len(store.auditActions) != 2 || store.auditActions[0] != "torrent.inspect" || store.auditActions[1] != "torrent.files" {
		t.Fatalf("audit actions = %#v", store.auditActions)
	}
}

func TestManagerDispatchesTransmissionRuntime(t *testing.T) {
	const sessionID = "manager-transmission-session"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Transmission-Session-Id") != sessionID {
			w.Header().Set("X-Transmission-Session-Id", sessionID)
			http.Error(w, "session required", http.StatusConflict)
			return
		}
		var request struct {
			Tag int64 `json:"tag"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"result": "success", "tag": request.Tag,
			"arguments": map[string]any{"version": "4.0.6", "rpc-version": 17, "rpc-version-minimum": 14},
		})
	}))
	t.Cleanup(server.Close)
	store := &fakeConfigurationStore{runtime: integrations.RuntimeDownloader{
		Downloader:     integrations.Downloader{Name: "transmission", Adapter: "transmission", Enabled: true},
		EndpointConfig: integrations.EndpointConfig{Endpoint: server.URL, TimeoutSeconds: 5},
		Credentials:    map[string]string{},
	}}
	probe, err := NewManager(store).Probe(context.Background(), "transmission", workflow.Actor{Type: "test", ID: "manager"})
	if err != nil || probe.ApplicationVersion != "4.0.6" || store.healthStatus != "ready" {
		t.Fatalf("Probe() result/health/error = %#v/%s/%v", probe, store.healthStatus, err)
	}
}

func TestManagerDispatchesRTorrentRuntime(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "invalid", http.StatusBadRequest)
			return
		}
		value := "0.15.3"
		if strings.Contains(string(body), "system.library_version") {
			value = "0.15.2"
		}
		if strings.Contains(string(body), "system.methodExist") {
			_, _ = io.WriteString(w, `<?xml version="1.0"?><methodResponse><params><param><value><boolean>1</boolean></value></param></params></methodResponse>`)
			return
		}
		_, _ = io.WriteString(w, `<?xml version="1.0"?><methodResponse><params><param><value><string>`+value+`</string></value></param></params></methodResponse>`)
	}))
	t.Cleanup(server.Close)
	store := &fakeConfigurationStore{runtime: integrations.RuntimeDownloader{
		Downloader:     integrations.Downloader{Name: "rtorrent", Adapter: "rtorrent", Enabled: true},
		EndpointConfig: integrations.EndpointConfig{Endpoint: server.URL, TimeoutSeconds: 5},
		Credentials:    map[string]string{},
	}}
	probe, err := NewManager(store).Probe(context.Background(), "rtorrent", workflow.Actor{Type: "test", ID: "manager"})
	if err != nil || probe.ApplicationVersion != "0.15.3" || probe.WebAPIVersion != "XML-RPC (libtorrent 0.15.2)" || store.healthStatus != "ready" {
		t.Fatalf("Probe() result/health/error = %#v/%s/%v", probe, store.healthStatus, err)
	}
}

func TestMapPathUsesPathBoundary(t *testing.T) {
	mapping := integrations.PathMapping{RemotePath: "/remote/downloads", LocalPath: "/downloads"}
	if mapped, ok := mapPath("/remote/downloads/release/file.mkv", mapping); !ok || mapped != "/downloads/release/file.mkv" {
		t.Fatalf("mapPath() = %s/%t", mapped, ok)
	}
	if _, ok := mapPath("/remote/downloads-evil/file.mkv", mapping); ok {
		t.Fatal("mapPath() accepted a prefix without path boundary")
	}
}

func TestApplyConfiguredDefaultsIsStableAndDoesNotOverrideRequest(t *testing.T) {
	options := qbittorrent.AddOptions{Category: "job", Tags: []string{"task", "task"}}
	applyConfiguredDefaults(map[string]any{"category": "configured", "tag": "task", "label": "retorrent"}, &options)
	if options.Category != "job" || strings.Join(options.Tags, ",") != "task,retorrent" {
		t.Fatalf("options = %#v", options)
	}
	empty := qbittorrent.AddOptions{}
	applyConfiguredDefaults(map[string]any{"category": "legacy", "label": "retorrent"}, &empty)
	if empty.Category != "legacy" || strings.Join(empty.Tags, ",") != "retorrent" {
		t.Fatalf("empty options = %#v", empty)
	}
}
