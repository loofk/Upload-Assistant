package mteam

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeRuntimeSiteStore struct {
	runtime integrations.RuntimeSite
	actions []string
	details []map[string]any
}

func (store *fakeRuntimeSiteStore) GetRuntimeSite(context.Context, string) (integrations.RuntimeSite, error) {
	return store.runtime, nil
}

func (store *fakeRuntimeSiteStore) AuditSiteAction(_ context.Context, _ string, action string, details map[string]any, _ workflow.Actor) error {
	store.actions = append(store.actions, action)
	store.details = append(store.details, details)
	return nil
}

func TestClientChecksMTeamDuplicatesWithoutCredentialLeak(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/torrent/search" || r.URL.RawQuery != "" || r.Header.Get("x-api-key") != "mteam-secret" {
			t.Fatalf("request path/query/key = %s/%s/%s", r.URL.Path, r.URL.RawQuery, r.Header.Get("x-api-key"))
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil || payload["imdb"] != "tt1234567" || payload["pageSize"] != float64(100) {
			t.Fatalf("search payload/error = %#v/%v", payload, err)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"code": "0", "data": map[string]any{"data": []map[string]any{{
				"id": 42, "name": "Fixture.2026.1080p", "size": 123456789,
				"category": 419, "standard": 1, "imdb": "tt1234567", "createdDate": "2026-08-08 12:00:00",
			}}},
		})
	}))
	defer server.Close()
	store := runtimeSiteStore(server.URL, "mteam-secret")
	evidence, err := NewClient(store, nil).DuplicateCheck(context.Background(), DuplicateQuery{IMDbID: "TT1234567"}, workflow.Actor{Type: "test", ID: "dupe"})
	if err != nil {
		t.Fatal(err)
	}
	if !evidence.Duplicate || evidence.ResultCount != 1 || evidence.Candidates[0].ID != "42" ||
		evidence.Candidates[0].SizeBytes != 123456789 || len(store.actions) != 1 {
		t.Fatalf("duplicate evidence/store = %#v/%#v", evidence, store)
	}
	encoded, _ := json.Marshal(evidence)
	if strings.Contains(string(encoded), "mteam-secret") {
		t.Fatal("duplicate evidence exposed API key")
	}
}

func TestClientReturnsAuditedCleanResult(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"code":0,"data":[]}`))
	}))
	defer server.Close()
	store := runtimeSiteStore(server.URL, "key")
	evidence, err := NewClient(store, nil).DuplicateCheck(context.Background(), DuplicateQuery{IMDbID: "tt7654321"}, workflow.Actor{})
	if err != nil || evidence.Duplicate || evidence.ResultCount != 0 || len(store.actions) != 1 {
		t.Fatalf("clean evidence/error/store = %#v/%v/%#v", evidence, err, store)
	}
}

func TestClientFailsClosedWithoutIdentityOrAPIKey(t *testing.T) {
	store := runtimeSiteStore("https://api.m-team.cc", "")
	_, err := NewClient(store, nil).DuplicateCheck(context.Background(), DuplicateQuery{}, workflow.Actor{})
	code, _, _ := sites.ErrorDetails(err)
	if code != "target_duplicate_identity_required" {
		t.Fatalf("missing identity error = %q/%v", code, err)
	}
	_, err = NewClient(store, nil).DuplicateCheck(context.Background(), DuplicateQuery{IMDbID: "tt1234567"}, workflow.Actor{})
	code, _, _ = sites.ErrorDetails(err)
	if code != "site_api_key_required" {
		t.Fatalf("missing API key error = %q/%v", code, err)
	}
}

func runtimeSiteStore(endpoint, apiKey string) *fakeRuntimeSiteStore {
	return &fakeRuntimeSiteStore{runtime: integrations.RuntimeSite{
		ID: "site-id", Code: "MTEAM", Name: "M-Team", Adapter: "mteam_api",
		Config:              json.RawMessage(`{"endpoint":"` + endpoint + `","timeout_seconds":30}`),
		ConfigurationSHA256: strings.Repeat("a", 64), UpdatedAt: time.Unix(1, 0).UTC(),
		Credentials: map[string]string{"api_key": apiKey},
	}}
}
