package downloaders

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeConfigurationStore struct {
	runtime         integrations.RuntimeDownloader
	healthStatus    string
	auditActions    []string
	failAuditAction string
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
	if action == store.failAuditAction {
		return errors.New("fixture audit failure")
	}
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
				"upspeed": 2048, "ratio": 2.5, "category": "MTEAM", "added_on": 100,
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
	dashboard, err := manager.Dashboard(context.Background(), "qbit", DashboardQuery{Filter: "active", Query: "release", Limit: 25})
	if err != nil || dashboard.Summary.Total != 1 || dashboard.Summary.UploadSpeed != 2048 || dashboard.FilteredTotal != 1 || len(dashboard.Torrents) != 1 || dashboard.Torrents[0].Category != "MTEAM" {
		t.Fatalf("Dashboard() snapshot/error = %#v/%v", dashboard, err)
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

func TestManagerDispatchesDelugeWebRuntime(t *testing.T) {
	methods := []string{
		"core.add_torrent_file", "core.get_torrent_status", "core.get_torrents_status", "core.set_torrent_options",
		"daemon.get_version", "daemon.get_method_list", "core.get_libtorrent_version",
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			Method string `json:"method"`
			ID     int64  `json:"id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "invalid", http.StatusBadRequest)
			return
		}
		var result any
		switch request.Method {
		case "auth.login", "web.connected":
			result = true
		case "daemon.get_method_list":
			result = methods
		case "daemon.get_version":
			result = "2.2.0"
		case "core.get_libtorrent_version":
			result = "2.0.11.0"
		default:
			http.Error(w, "unexpected", http.StatusBadRequest)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"result": result, "error": nil, "id": request.ID})
	}))
	t.Cleanup(server.Close)
	store := &fakeConfigurationStore{runtime: integrations.RuntimeDownloader{
		Downloader:     integrations.Downloader{Name: "deluge", Adapter: "deluge", Enabled: true},
		EndpointConfig: integrations.EndpointConfig{Endpoint: server.URL, TimeoutSeconds: 5},
		Credentials:    map[string]string{"password": "secret"},
	}}
	probe, err := NewManager(store).Probe(context.Background(), "deluge", workflow.Actor{Type: "test", ID: "manager"})
	if err != nil || probe.ApplicationVersion != "Deluge 2.2.0" || probe.Authentication != "deluge-web-cookie" || store.healthStatus != "ready" {
		t.Fatalf("Probe() result/health/error = %#v/%s/%v", probe, store.healthStatus, err)
	}
}

func TestManagerRejectsUnsupportedConfiguredDelugeLabelsBeforeRemoteCall(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		http.Error(w, "unexpected", http.StatusInternalServerError)
	}))
	t.Cleanup(server.Close)
	store := &fakeConfigurationStore{runtime: integrations.RuntimeDownloader{
		Downloader: integrations.Downloader{Name: "deluge", Adapter: "deluge", Enabled: true},
		EndpointConfig: integrations.EndpointConfig{
			Endpoint: server.URL, TimeoutSeconds: 5, Options: map[string]any{"category": "legacy"},
		},
		Credentials: map[string]string{"password": "secret"},
	}}
	metainfo := validManagerMetainfo()
	_, err := NewManager(store).Add(context.Background(), "deluge", metainfo, qbittorrent.AddOptions{}, workflow.Actor{Type: "test", ID: "manager"})
	if !errors.Is(err, integrations.ErrValidation) || !strings.Contains(err.Error(), "category") {
		t.Fatalf("Add() capability error = %v", err)
	}
	if calls != 0 {
		t.Fatalf("Add() made %d calls before capability validation", calls)
	}
}

func TestManagerPersistsAddIntentBeforeRemoteWrite(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		http.Error(w, "unexpected", http.StatusInternalServerError)
	}))
	t.Cleanup(server.Close)
	store := qBittorrentAddStore(t, server.URL)
	store.failAuditAction = "torrent.add_intent"
	metainfo := validManagerMetainfo()
	evidence, err := NewManager(store).Add(context.Background(), "qbit", metainfo, qbittorrent.AddOptions{}, workflow.Actor{Type: "test", ID: "manager"})
	if err == nil || !strings.Contains(err.Error(), "persist downloader add intent") || evidence.TorrentSHA256 != "" {
		t.Fatalf("Add() evidence/error = %#v/%v", evidence, err)
	}
	if calls != 0 || strings.Join(store.auditActions, ",") != "torrent.add_intent" {
		t.Fatalf("remote calls/audits = %d/%#v", calls, store.auditActions)
	}
}

func TestManagerReturnsEvidenceWhenAddResultAuditIsUnknown(t *testing.T) {
	metainfo := validManagerMetainfo()
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	addCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v2/torrents/add":
			addCalls++
			_, _ = io.WriteString(w, "Ok.")
		case "/api/v2/torrents/info":
			_ = json.NewEncoder(w).Encode([]map[string]any{{
				"hash": hashes.V1SHA1, "name": "test", "state": "downloading", "progress": 0,
				"total_size": 1, "save_path": "/downloads", "content_path": "/downloads/test",
			}})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	store := qBittorrentAddStore(t, server.URL)
	store.failAuditAction = "torrent.add"
	evidence, err := NewManager(store).Add(context.Background(), "qbit", metainfo, qbittorrent.AddOptions{SavePath: "/downloads"}, workflow.Actor{Type: "test", ID: "manager"})
	if !errors.Is(err, ErrAddOutcomeUnknown) || evidence.Result.Hashes != hashes || evidence.Observed == nil || evidence.TorrentSHA256 == "" {
		t.Fatalf("Add() evidence/error = %#v/%v", evidence, err)
	}
	if addCalls != 1 || strings.Join(store.auditActions, ",") != "torrent.add_intent,torrent.add" {
		t.Fatalf("remote add calls/audits = %d/%#v", addCalls, store.auditActions)
	}
}

func TestManagerAppliesSeedboxUploadLimitOnlyToHumanMarkedSeedbox(t *testing.T) {
	metainfo := validManagerMetainfo()
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name, networkClass string
		wantLimit          int64
	}{{"seedbox", "seedbox", 2 * 1024 * 1024}, {"home", "home", 0}, {"unknown", "unknown", 0}} {
		t.Run(test.name, func(t *testing.T) {
			var submitted string
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/api/v2/torrents/add":
					if err := r.ParseMultipartForm(1 << 20); err != nil {
						http.Error(w, err.Error(), http.StatusBadRequest)
						return
					}
					submitted = r.FormValue("upLimit")
					_, _ = io.WriteString(w, "Ok.")
				case "/api/v2/torrents/info":
					_ = json.NewEncoder(w).Encode([]map[string]any{{
						"hash": hashes.V1SHA1, "name": "test", "state": "downloading", "progress": 0,
						"total_size": 1, "save_path": "/downloads", "content_path": "/downloads/test", "up_limit": test.wantLimit,
					}})
				default:
					http.NotFound(w, r)
				}
			}))
			t.Cleanup(server.Close)
			store := qBittorrentAddStore(t, server.URL)
			store.runtime.NetworkClass = test.networkClass
			evidence, err := NewManager(store).Add(context.Background(), "qbit", metainfo, qbittorrent.AddOptions{
				SavePath: "/downloads", SeedboxUploadLimit: 2 * 1024 * 1024,
			}, workflow.Actor{Type: "test", ID: "manager"})
			if err != nil {
				t.Fatal(err)
			}
			if evidence.NetworkClass != test.networkClass || evidence.AppliedUploadLimit != test.wantLimit || submitted != fmt.Sprint(test.wantLimit) {
				t.Fatalf("seedbox evidence/submission = %#v/%q", evidence, submitted)
			}
		})
	}
}

func TestManagerClassifiesUntrustworthyRemoteAddResponse(t *testing.T) {
	metainfo := validManagerMetainfo()
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v2/torrents/add" {
			http.Error(w, "ambiguous upstream failure", http.StatusInternalServerError)
			return
		}
		http.NotFound(w, r)
	}))
	t.Cleanup(server.Close)
	store := qBittorrentAddStore(t, server.URL)
	evidence, err := NewManager(store).Add(context.Background(), "qbit", metainfo, qbittorrent.AddOptions{}, workflow.Actor{Type: "test", ID: "manager"})
	if !errors.Is(err, ErrAddOutcomeUnknown) || evidence.Result.Hashes != hashes || evidence.TorrentSHA256 == "" {
		t.Fatalf("Add() evidence/error = %#v/%v", evidence, err)
	}
	if strings.Join(store.auditActions, ",") != "torrent.add_intent" {
		t.Fatalf("audits = %#v", store.auditActions)
	}
}

func TestManagerPersistsLimitIntentBeforeRemoteWrite(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		http.Error(w, "unexpected", http.StatusInternalServerError)
	}))
	t.Cleanup(server.Close)
	store := qBittorrentAddStore(t, server.URL)
	store.failAuditAction = "torrent.set_limits_intent"
	evidence, err := NewManager(store).SetLimits(
		context.Background(), "qbit", strings.Repeat("a", 40), 4096, 8192, workflow.Actor{Type: "test", ID: "manager"},
	)
	if err == nil || !strings.Contains(err.Error(), "persist downloader limit intent") || evidence.DownloaderName != "" {
		t.Fatalf("SetLimits() evidence/error = %#v/%v", evidence, err)
	}
	if calls != 0 || strings.Join(store.auditActions, ",") != "torrent.set_limits_intent" {
		t.Fatalf("remote calls/audits = %d/%#v", calls, store.auditActions)
	}
}

func TestManagerClassifiesPartialLimitWriteAsUnknown(t *testing.T) {
	hash := strings.Repeat("a", 40)
	setCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v2/torrents/setDownloadLimit":
			setCalls++
			_, _ = io.WriteString(w, "Ok.")
		case "/api/v2/torrents/setUploadLimit":
			setCalls++
			http.Error(w, "ambiguous upstream failure", http.StatusInternalServerError)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	store := qBittorrentAddStore(t, server.URL)
	evidence, err := NewManager(store).SetLimits(
		context.Background(), "qbit", hash, 4096, 8192, workflow.Actor{Type: "test", ID: "manager"},
	)
	if !errors.Is(err, ErrLimitsOutcomeUnknown) || evidence.DownloaderName != "" || setCalls != 2 {
		t.Fatalf("SetLimits() evidence/error/calls = %#v/%v/%d", evidence, err, setCalls)
	}
	if strings.Join(store.auditActions, ",") != "torrent.set_limits_intent" {
		t.Fatalf("audits = %#v", store.auditActions)
	}
}

func TestManagerReturnsLimitEvidenceWhenResultAuditFails(t *testing.T) {
	hash := strings.Repeat("a", 40)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v2/torrents/setDownloadLimit", "/api/v2/torrents/setUploadLimit":
			_, _ = io.WriteString(w, "Ok.")
		case "/api/v2/torrents/info":
			_ = json.NewEncoder(w).Encode([]map[string]any{{
				"hash": hash, "name": "test", "state": "uploading", "progress": 1,
				"total_size": 1, "save_path": "/downloads", "content_path": "/downloads/test",
				"dl_limit": 4096, "up_limit": 8192,
			}})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	store := qBittorrentAddStore(t, server.URL)
	store.failAuditAction = "torrent.set_limits"
	evidence, err := NewManager(store).SetLimits(
		context.Background(), "qbit", hash, 4096, 8192, workflow.Actor{Type: "test", ID: "manager"},
	)
	if !errors.Is(err, ErrLimitsOutcomeUnknown) || evidence.DownloaderName != "qbit" || evidence.Torrent.Hash != hash ||
		evidence.Torrent.DownloadLimit != 4096 || evidence.Torrent.UploadLimit != 8192 {
		t.Fatalf("SetLimits() evidence/error = %#v/%v", evidence, err)
	}
	if strings.Join(store.auditActions, ",") != "torrent.set_limits_intent,torrent.set_limits" {
		t.Fatalf("audits = %#v", store.auditActions)
	}
}

func TestManagerValidatesLimitRequestBeforeIntent(t *testing.T) {
	store := qBittorrentAddStore(t, "http://127.0.0.1:1")
	manager := NewManager(store)
	for _, fixture := range []struct {
		hash     string
		download int64
	}{
		{"invalid", 0},
		{strings.Repeat("a", 40), -1},
	} {
		if _, err := manager.SetLimits(context.Background(), "qbit", fixture.hash, fixture.download, 0, workflow.Actor{Type: "test", ID: "manager"}); !errors.Is(err, integrations.ErrValidation) {
			t.Fatalf("SetLimits(%q, %d) error = %v", fixture.hash, fixture.download, err)
		}
	}
	if len(store.auditActions) != 0 {
		t.Fatalf("invalid request audits = %#v", store.auditActions)
	}
}

func qBittorrentAddStore(t *testing.T, endpoint string) *fakeConfigurationStore {
	t.Helper()
	capability, ok := integrations.DownloaderAdapterCapabilityFor("qbittorrent")
	if !ok {
		t.Fatal("qBittorrent capability is missing")
	}
	return &fakeConfigurationStore{runtime: integrations.RuntimeDownloader{
		Downloader:     integrations.Downloader{Name: "qbit", Adapter: "qbittorrent", Enabled: true, AdapterCapability: capability},
		EndpointConfig: integrations.EndpointConfig{Endpoint: endpoint, TimeoutSeconds: 5},
		Credentials:    map[string]string{"api_key": "qbt_test"}, ConfigurationSHA256: strings.Repeat("a", 64),
	}}
}

func validManagerMetainfo() []byte {
	return []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:test12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaaee")
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
	disabled := false
	withoutLabels := qbittorrent.AddOptions{ApplyLabels: &disabled}
	applyConfiguredDefaults(map[string]any{"category": "legacy", "label": "retorrent"}, &withoutLabels)
	if withoutLabels.Category != "" || len(withoutLabels.Tags) != 0 {
		t.Fatalf("explicit no-label options = %#v", withoutLabels)
	}
}

func TestValidateAddCapabilitiesRequiresExplicitNoLabelModeForDeluge(t *testing.T) {
	capability, _ := integrations.DownloaderAdapterCapabilityFor("deluge")
	if err := validateAddCapabilities(capability, qbittorrent.AddOptions{}); !errors.Is(err, integrations.ErrValidation) || !strings.Contains(err.Error(), "apply_labels=false") {
		t.Fatalf("implicit Deluge labels error = %v", err)
	}
	disabled := false
	if err := validateAddCapabilities(capability, qbittorrent.AddOptions{ApplyLabels: &disabled}); err != nil {
		t.Fatalf("explicit Deluge no-label error = %v", err)
	}
	if err := validateAddCapabilities(capability, qbittorrent.AddOptions{ApplyLabels: &disabled, Category: "source"}); !errors.Is(err, integrations.ErrValidation) {
		t.Fatalf("contradictory Deluge no-label error = %v", err)
	}
}
