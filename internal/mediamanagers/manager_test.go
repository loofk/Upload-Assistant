package mediamanagers

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeStore struct {
	runtime integrations.RuntimeMediaManager
	health  map[string]any
	audit   map[string]any
}

func (store *fakeStore) GetRuntimeMediaManager(context.Context, string) (integrations.RuntimeMediaManager, error) {
	return store.runtime, nil
}

func (store *fakeStore) RecordMediaManagerHealth(_ context.Context, _, _ string, details map[string]any, _ workflow.Actor) error {
	store.health = details
	return nil
}

func (store *fakeStore) AuditMediaManagerAction(_ context.Context, _, _ string, details map[string]any, _ workflow.Actor) error {
	store.audit = details
	return nil
}

func TestManagerProbeAndSonarrLookup(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Header.Get("X-Api-Key") != "fixture-key" {
			http.Error(response, "missing key", http.StatusUnauthorized)
			return
		}
		response.Header().Set("Content-Type", "application/json")
		switch request.URL.Path {
		case "/sonarr/api/v3/system/status":
			_, _ = response.Write([]byte(`{"version":"4.0.15","appName":"Sonarr","instanceName":"盒子"}`))
		case "/sonarr/api/v3/parse":
			if request.URL.Query().Get("path") != "/downloads/Show.S01E01.mkv" || request.URL.Query().Get("title") == "" {
				t.Fatalf("unexpected query %s", request.URL.RawQuery)
			}
			_, _ = response.Write([]byte(`{"series":{"title":"示例动画","year":2026,"tvdbId":123,"tmdbId":456,"tvMazeId":789,"imdbId":"tt0011223","genres":["Anime"]},"parsedEpisodeInfo":{"releaseGroup":"U2"}}`))
		default:
			http.NotFound(response, request)
		}
	}))
	defer server.Close()
	store := &fakeStore{runtime: integrations.RuntimeMediaManager{
		MediaManager:        integrations.MediaManager{Name: "sonarr-main", Adapter: "sonarr", Enabled: true},
		EndpointConfig:      integrations.EndpointConfig{Endpoint: server.URL + "/sonarr", TimeoutSeconds: 5},
		ConfigurationSHA256: strings.Repeat("a", 64), Credentials: map[string]string{"api_key": "fixture-key"},
	}}
	manager := NewManager(store, server.Client())
	probe, err := manager.Probe(context.Background(), "sonarr-main", workflow.Actor{Type: "test", ID: "fixture"})
	if err != nil || probe.Version != "4.0.15" || store.health["response_sha256"] == "" {
		t.Fatalf("Probe() = %#v, health=%#v, err=%v", probe, store.health, err)
	}
	lookup, err := manager.Lookup(context.Background(), "sonarr-main", LookupRequest{Path: "/downloads/Show.S01E01.mkv", Title: "Show.S01E01"}, workflow.Actor{Type: "test", ID: "fixture"})
	if err != nil || !lookup.Matched || lookup.Metadata.TVDBID != 123 || lookup.Metadata.IMDbID != "tt0011223" || lookup.Metadata.ReleaseGroup != "U2" {
		t.Fatalf("Lookup() = %#v, err=%v", lookup, err)
	}
	encoded, _ := json.Marshal(store.audit)
	if strings.Contains(string(encoded), "/downloads/") || !strings.Contains(string(encoded), "query_sha256") {
		t.Fatalf("audit must contain hashes but not paths: %s", encoded)
	}
}

func TestManagerRadarrLookupRequiresExactPath(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v3/movie/lookup" || request.URL.Query().Get("term") == "" {
			t.Fatalf("request = %s?%s", request.URL.Path, request.URL.RawQuery)
		}
		_, _ = response.Write([]byte(`[{"title":"Wrong","tmdbId":1,"movieFile":{"originalFilePath":"/other.mkv"}},{"title":"电影","year":2025,"tmdbId":99,"imdbId":"tt7654321","genres":["Drama"],"movieFile":{"originalFilePath":"/downloads/Movie.mkv","releaseGroup":"CHD"}}]`))
	}))
	defer server.Close()
	store := &fakeStore{runtime: integrations.RuntimeMediaManager{
		MediaManager:        integrations.MediaManager{Name: "radarr-main", Adapter: "radarr", Enabled: true},
		EndpointConfig:      integrations.EndpointConfig{Endpoint: server.URL, TimeoutSeconds: 5},
		ConfigurationSHA256: strings.Repeat("b", 64), Credentials: map[string]string{"api_key": "fixture-key"},
	}}
	result, err := NewManager(store, server.Client()).Lookup(context.Background(), "radarr-main", LookupRequest{Path: "/downloads/Movie.mkv"}, workflow.Actor{Type: "test"})
	if err != nil || result.Metadata.TMDBID != 99 || result.Metadata.ReleaseGroup != "CHD" {
		t.Fatalf("Lookup() = %#v, err=%v", result, err)
	}
}

func TestManagerRejectsIncompleteLookup(t *testing.T) {
	store := &fakeStore{runtime: integrations.RuntimeMediaManager{MediaManager: integrations.MediaManager{Name: "sonarr", Adapter: "sonarr", Enabled: true}}}
	_, err := NewManager(store, nil).Lookup(context.Background(), "sonarr", LookupRequest{Path: "/downloads/a.mkv"}, workflow.Actor{})
	if !errors.Is(err, ErrValidation) {
		t.Fatalf("Lookup() error = %v", err)
	}
}
