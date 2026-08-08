package metadataproviders

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeStore struct {
	runtime integrations.RuntimeMetadataProvider
	audits  []map[string]any
	health  []map[string]any
}

func (store *fakeStore) GetRuntimeMetadataProvider(context.Context, string) (integrations.RuntimeMetadataProvider, error) {
	return store.runtime, nil
}

func (store *fakeStore) RecordMetadataProviderHealth(_ context.Context, _ string, status string, details map[string]any, _ workflow.Actor) error {
	copy := map[string]any{"status": status}
	for key, value := range details {
		copy[key] = value
	}
	store.health = append(store.health, copy)
	return nil
}

func (store *fakeStore) AuditMetadataProviderAction(_ context.Context, _, _ string, details map[string]any, _ workflow.Actor) error {
	store.audits = append(store.audits, details)
	return nil
}

func TestTMDbFindByIMDbNormalizesOneMovieAndAuditsHashes(t *testing.T) {
	const secret = "tmdb-secret-never-audited"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/3/find/tt1234567" || request.URL.Query().Get("external_source") != "imdb_id" || request.URL.Query().Get("api_key") != secret {
			t.Fatalf("unexpected request %s?%s", request.URL.Path, request.URL.RawQuery)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"movie_results": []any{map[string]any{"id": 42}}, "tv_results": []any{}})
	}))
	defer server.Close()
	store := &fakeStore{runtime: integrations.RuntimeMetadataProvider{
		MetadataProvider:    integrations.MetadataProvider{Name: "tmdb-main", Adapter: "tmdb", Enabled: true},
		EndpointConfig:      integrations.EndpointConfig{Endpoint: server.URL, TimeoutSeconds: 2, Options: map[string]any{"language": "zh-CN"}},
		ConfigurationSHA256: strings.Repeat("a", 64), Credentials: map[string]string{"api_key": secret},
	}}
	result, err := NewManager(store, server.Client()).Resolve(context.Background(), "tmdb-main", ResolveRequest{IMDbID: "1234567"}, workflow.Actor{Type: "test"})
	if err != nil {
		t.Fatal(err)
	}
	if !result.Matched || result.Identity.IMDbID != "tt1234567" || result.Identity.TMDbID != "42" || result.Identity.TMDbType != "movie" || len(result.Calls) != 1 || result.Calls[0].Sequence != 1 {
		t.Fatalf("unexpected result: %#v", result)
	}
	auditJSON, _ := json.Marshal(store.audits)
	if strings.Contains(string(auditJSON), secret) || len(store.health) != 1 || store.health[0]["status"] != "ready" {
		t.Fatalf("secret leaked or health missing: %s / %#v", auditJSON, store.health)
	}
}

func TestTMDbRejectsAmbiguousFindResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"movie_results":[{"id":1}],"tv_results":[{"id":2}]}`))
	}))
	defer server.Close()
	store := &fakeStore{runtime: integrations.RuntimeMetadataProvider{
		MetadataProvider: integrations.MetadataProvider{Name: "tmdb", Adapter: "tmdb", Enabled: true},
		EndpointConfig:   integrations.EndpointConfig{Endpoint: server.URL, TimeoutSeconds: 2}, Credentials: map[string]string{"api_key": "x"},
	}}
	_, err := NewManager(store, server.Client()).Resolve(context.Background(), "tmdb", ResolveRequest{IMDbID: "tt1234567"}, workflow.Actor{})
	if err == nil || !strings.Contains(err.Error(), "ambiguous") || len(store.audits) != 0 || len(store.health) != 1 || store.health[0]["status"] != "failed" {
		t.Fatalf("error/audit/health = %v/%#v/%#v", err, store.audits, store.health)
	}
}

func TestPTGenUsesIMDbThenDiscoveredDoubanWithoutLeakingKey(t *testing.T) {
	const secret = "ptgen-secret-never-audited"
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		requests++
		if request.Method != http.MethodPost || request.URL.Path != "/api" || request.URL.Query().Get("key") != secret {
			t.Fatalf("unexpected PTGen request: %s %s", request.Method, request.URL.String())
		}
		if requests == 1 {
			if request.URL.Query().Get("sid") != "tt7654321" || request.URL.Query().Get("source") != "imdb" {
				t.Fatalf("unexpected discovery query: %s", request.URL.RawQuery)
			}
			_, _ = w.Write([]byte(`{"success":true,"data":{"douban":"https://movie.douban.com/subject/1292052/"},"format":"discovery"}`))
			return
		}
		if request.URL.Query().Get("url") != "https://movie.douban.com/subject/1292052/" {
			t.Fatalf("unexpected Douban query: %s", request.URL.RawQuery)
		}
		_, _ = w.Write([]byte(`{"success":true,"format":"[img]poster[/img]\n[b]豆瓣简介[/b]"}`))
	}))
	defer server.Close()
	store := &fakeStore{runtime: integrations.RuntimeMetadataProvider{
		MetadataProvider:    integrations.MetadataProvider{Name: "ptgen-main", Adapter: "ptgen", Enabled: true},
		EndpointConfig:      integrations.EndpointConfig{Endpoint: server.URL, TimeoutSeconds: 2},
		ConfigurationSHA256: strings.Repeat("b", 64), Credentials: map[string]string{"api_key": secret},
	}}
	result, err := NewManager(store, server.Client()).Resolve(context.Background(), "ptgen-main", ResolveRequest{IMDbID: "tt7654321"}, workflow.Actor{Type: "test"})
	if err != nil {
		t.Fatal(err)
	}
	if requests != 2 || result.Identity.DoubanID != "1292052" || result.Description == "" || len(result.Calls) != 2 || result.Calls[1].Sequence != 2 {
		t.Fatalf("unexpected result: %#v / requests=%d", result, requests)
	}
	auditJSON, _ := json.Marshal(store.audits)
	if strings.Contains(string(auditJSON), secret) || strings.Contains(string(auditJSON), "豆瓣简介") {
		t.Fatalf("audit leaked credential or description: %s", auditJSON)
	}
}

func TestValidationRequiresTypedTMDbID(t *testing.T) {
	_, err := NewManager(&fakeStore{}, nil).Resolve(context.Background(), "provider", ResolveRequest{TMDbID: "42"}, workflow.Actor{})
	if err == nil || !strings.Contains(err.Error(), "tmdb_type") {
		t.Fatalf("Resolve() error = %v", err)
	}
}
